import os
import hashlib
import smtplib
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, ConfigDict
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, text, event, func, Boolean
from sqlalchemy.orm import sessionmaker, Session, relationship, declarative_base
from sqlalchemy.engine import Engine
from dotenv import load_dotenv

# ROOT CAUSE FIX 2b: load_dotenv(override=True) forces .env values to overwrite any
# stale environment variables already set in the shell. Without override=True, if
# AWS_ACCESS_KEY_ID was previously set to "" or an old value in the shell environment,
# load_dotenv() silently keeps the shell value and .env is ignored.
# verbose=True prints which .env file was loaded so startup logs confirm it.
load_dotenv(override=True, verbose=True)
print("ENV FILE LOADED")
print("AWS KEY FOUND:", os.getenv("AWS_ACCESS_KEY_ID") is not None)
print("AWS SECRET FOUND:", os.getenv("AWS_SECRET_ACCESS_KEY") is not None)
app = FastAPI(title="Online Proctored Exam Service", version="1.0.0")

# ================================================================
# Configuration Constants
# ================================================================
MAX_UPLOAD_QUESTIONS = int(os.getenv("MAX_UPLOAD_QUESTIONS", "50000"))  # 50,000 questions per category
MAX_ATTEMPTS_PER_TEST = int(os.getenv("MAX_ATTEMPTS_PER_TEST", "3"))    # 3 attempts per test
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status": "healthy"}

# -------------------------------------------------------------
# Startup AWS Credential Validation
# Prints at boot so missing .env values are visible immediately.
# -------------------------------------------------------------
@app.on_event("startup")
def validate_aws_credentials():
    print("\n[FastAPI Startup] === AWS Credential Check ===")
    checks = {
        "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID"),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY"),
        "AWS_REGION": os.getenv("AWS_REGION", "eu-north-1"),
        "S3_BUCKET_NAME": os.getenv("S3_BUCKET_NAME", "chakorahub-exam-recordings"),
        "SES_SENDER_EMAIL": os.getenv("SES_SENDER_EMAIL"),
        "ADMIN_EMAIL": os.getenv("ADMIN_EMAIL"),
    }
    all_ok = True
    for key, val in checks.items():
        if val:
            display = f"{val[:6]}..." if len(val or "") > 6 else val
            print(f"[FastAPI Startup]   {key}: SET ({display})")
        else:
            print(f"[FastAPI Startup]   {key}: *** MISSING — S3/SES will fall back ***")
            all_ok = False
    if all_ok:
        print("[FastAPI Startup] All AWS credentials present. S3 and SES are enabled.")
    else:
        print("[FastAPI Startup] WARNING: Some credentials missing. Add them to your .env file.")
    print("[FastAPI Startup] ================================\n")

# ================================================================
# Database Connection & Engine Configuration (ORACLE)
# ================================================================

# Oracle-only mode (no SQLite fallback)
ORACLE_SERVICE = os.getenv("ORACLE_SERVICE", os.getenv("ORACLE_SERVICE_NAME", "FREEPDB1"))
ORACLE_HOST = os.getenv("ORACLE_HOST", "56.228.73.210")
ORACLE_PORT = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_USER = os.getenv("ORACLE_USER", "SUPPORT")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "Welcome123")
ORACLE_SCHEMA = os.getenv("ORACLE_SCHEMA", "CHAKORA")

if not ORACLE_USER or not ORACLE_PASSWORD:
    raise RuntimeError("Oracle mode requires ORACLE_USER and ORACLE_PASSWORD environment variables.")

print(f"Connecting to Oracle: {ORACLE_HOST}:{ORACLE_PORT}/{ORACLE_SERVICE}")
print(f"Oracle user: {ORACLE_USER}")
print(f"Oracle schema: {ORACLE_SCHEMA}")

DATABASE_URL = f"oracle+oracledb://{ORACLE_USER}:{ORACLE_PASSWORD}@{ORACLE_HOST}:{ORACLE_PORT}/?service_name={ORACLE_SERVICE}"

# Set Oracle schema on every new connection.
@event.listens_for(Engine, "connect")
def set_oracle_session_schema(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {ORACLE_SCHEMA}")
        cursor.close()
    except Exception as schema_err:
        # Keep startup alive but print the reason so schema issues are visible in PM2 logs.
        print(f"Oracle schema switch failed: {schema_err}")

try:
    engine = create_engine(DATABASE_URL, echo=False)
    with engine.connect() as conn:
        conn.execute(text(f"ALTER SESSION SET CURRENT_SCHEMA = {ORACLE_SCHEMA}"))
        conn.execute(text("SELECT 1 FROM DUAL"))
    print("Oracle Database connection test: SUCCESS")
except Exception as oracle_err:
    raise RuntimeError(f"Oracle connection failed: {str(oracle_err)}")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ================================================================
# SQLAlchemy Models (All existing models - UNCHANGED)
# ================================================================

class QuestionBankModel(Base):
    __tablename__ = "question_bank"
    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(100), nullable=False)
    question_text = Column(String(2000), nullable=False)
    option_a = Column(String(500), nullable=False)
    option_b = Column(String(500), nullable=False)
    option_c = Column(String(500), nullable=False)
    option_d = Column(String(500), nullable=False)
    correct_option = Column(String(1), nullable=False)
    explanation = Column(String(2000), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

class MockTestModel(Base):
    __tablename__ = "mock_tests"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    category = Column(String(100), nullable=False)
    description = Column(String(1000), nullable=True)
    duration_minutes = Column(Integer, nullable=False)
    scheduled_date = Column(String(20), nullable=False)  # YYYY-MM-DD
    start_time = Column(String(10), nullable=False)  # HH:MM
    end_time = Column(String(10), nullable=False)  # HH:MM
    is_published = Column(Integer, default=1)  # Auto-publish by default (1 = published)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class MockTestQuestionModel(Base):
    __tablename__ = "mock_test_questions"
    id = Column(Integer, primary_key=True, index=True)
    mock_test_id = Column(Integer, ForeignKey("mock_tests.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("question_bank.id", ondelete="CASCADE"), nullable=False, index=True)
    question_order = Column(Integer, nullable=True)

class UserTestAttemptModel(Base):
    __tablename__ = "user_test_attempts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    mock_test_id = Column(Integer, ForeignKey("mock_tests.id"), nullable=False, index=True)
    attempt_date = Column(DateTime, default=datetime.utcnow)
    score = Column(Integer, nullable=True)
    total_questions = Column(Integer, nullable=True)
    percentage = Column(Float, nullable=True)
    answers = Column(String(5000), nullable=True)  # JSON string
    recording_url = Column(String(500), nullable=True)
    status = Column(String(50), default="in_progress")

# -------------------------------------------------------------
# SQLAlchemy Models
# -------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)
    email = Column(String(100), nullable=False)
    role = Column(String(50), nullable=False) # 'admin' or 'candidate'

class ExamModel(Base):
    __tablename__ = "exams"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    description = Column(String(1000), nullable=True)
    duration_minutes = Column(Integer, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"))
    # CERTIFICATION = full proctoring, S3 recording, SES email, certificate eligible
    # MOCK_TEST     = webcam + S3 recording, instant feedback, unlimited attempts, no SES, no certificate
    exam_type = Column(String(20), nullable=False, default="CERTIFICATION")

class QuestionModel(Base):
    __tablename__ = "questions"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), nullable=False)
    question_text = Column(String(2000), nullable=False)
    option_a = Column(String(500), nullable=False)
    option_b = Column(String(500), nullable=False)
    option_c = Column(String(500), nullable=False)
    option_d = Column(String(500), nullable=False)
    correct_option = Column(String(1), nullable=False) # 'A', 'B', 'C', 'D'
    explanation = Column(String(2000), nullable=True)  # shown after answer in MOCK_TEST mode

class ExamAttemptModel(Base):
    __tablename__ = "exam_attempts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False, index=True)
    status = Column(String(50), default="started") # 'started', 'completed'
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    recording_url = Column(String(500), nullable=True)

class AnswerModel(Base):
    __tablename__ = "answers"
    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(Integer, ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False, index=True)
    selected_option = Column(String(1), nullable=True) # 'A', 'B', 'C', 'D' or None

class ResultModel(Base):
    __tablename__ = "results"
    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(Integer, ForeignKey("exam_attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    exam_id = Column(Integer, ForeignKey("exams.id"), nullable=False, index=True)
    total_questions = Column(Integer, nullable=False)
    correct_answers = Column(Integer, nullable=False)
    score = Column(Float, nullable=False) # raw score
    percentage = Column(Float, nullable=False)
    status = Column(String(50), nullable=False) # 'PASS', 'FAIL'

class ProctorLogModel(Base):
    __tablename__ = "proctor_logs"
    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, nullable=False, index=True)
    violation_type = Column(String(100), nullable=False)
    description = Column(String(1000), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    is_mock = Column(Boolean, default=False, nullable=False)

# ================================================================
# DATABASE SEEDING (UNCHANGED)
# ================================================================
def seed_database(db: Session):
    # Check if we have users already
    if db.query(UserModel).first() is not None:
        return
        
    print("Seeding database with default accounts and exams...")
    admin_pw = hashlib.sha256("admin123".encode()).hexdigest()
    candidate_pw = hashlib.sha256("candidate123".encode()).hexdigest()

    admin = UserModel(username="admin", password=admin_pw, email="admin@proctoredexampreview.com", role="admin")
    cand = UserModel(username="candidate", password=candidate_pw, email="candidate@proctoredexampreview.com", role="candidate")
    cand1 = UserModel(username="candidate1", password=candidate_pw, email="candidate1@proctoredexampreview.com", role="candidate")
    cand2 = UserModel(username="candidate2", password=candidate_pw, email="candidate2@proctoredexampreview.com", role="candidate")
    
    db.add_all([admin, cand, cand1, cand2])
    db.commit()

    # --- Certification Exam (existing, unchanged) ---
    exam = ExamModel(
        title="Python Basics Certification",
        description="Test your knowledge on Python syntax, data types, control flow, functions, and standard libraries.",
        duration_minutes=10,
        created_by=admin.id,
        exam_type="CERTIFICATION"
    )
    db.add(exam)
    db.commit()

    q1 = QuestionModel(exam_id=exam.id, question_text='What is the correct syntax to output "Hello World" in Python?', option_a='print("Hello World")', option_b='p("Hello World")', option_c='echo("Hello World")', option_d='printf("Hello World")', correct_option='A')
    q2 = QuestionModel(exam_id=exam.id, question_text='Which keyword is used to create a function in Python?', option_a='function', option_b='void', option_c='def', option_d='create', correct_option='C')
    q3 = QuestionModel(exam_id=exam.id, question_text='How do you insert comments in Python code?', option_a='# this is a comment', option_b='// this is a comment', option_c='/* this is a comment */', option_d='-- this is a comment', correct_option='A')
    q4 = QuestionModel(exam_id=exam.id, question_text='What is the correct file extension for Python files?', option_a='.pyt', option_b='.py', option_c='.pyw', option_d='.python', correct_option='B')
    q5 = QuestionModel(exam_id=exam.id, question_text='Which data type is mutable in Python?', option_a='tuple', option_b='list', option_c='string', option_d='int', correct_option='B')
    db.add_all([q1, q2, q3, q4, q5])
    db.commit()

    # --- Python Theory Mock Test (from uploaded MCQ files) ---
    mock = ExamModel(
        title="Python Theory Mock Test",
        description="50-question practice test covering Python OOP, generators, decorators, concurrency, async, functional programming, and advanced concepts. Instant explanations shown after each answer.",
        duration_minutes=60,
        created_by=admin.id,
        exam_type="MOCK_TEST"
    )
    db.add(mock)
    db.commit()

    mock_questions = [
        # --- From python_mcq_mock_2.txt (25 questions) ---
        QuestionModel(exam_id=mock.id, question_text="What is the primary role of __init__ in Python?", option_a="It creates a new class object in memory", option_b="It initializes a newly created object's attributes", option_c="It is called before the object is created", option_d="It returns the object to the caller", correct_option="B", explanation="Trap: Students confuse __init__ with __new__. __new__ creates, __init__ initializes."),
        QuestionModel(exam_id=mock.id, question_text="Duck typing in Python means:", option_a="Python checks the type of an object before calling its method", option_b="Only objects of the correct type can be passed to a function", option_c="If an object supports the required behavior, it can be used regardless of its type", option_d="Python automatically converts types at runtime", correct_option="C", explanation="Duck typing is about behavior, not automatic conversion."),
        QuestionModel(exam_id=mock.id, question_text="What does 'a is b' actually check?", option_a="Whether a and b have the same value", option_b="Whether a and b are of the same data type", option_c="Whether a and b point to the same object in memory", option_d="Whether a equals b after type casting", correct_option="C", explanation="== checks value; is checks identity. Small integers are cached, making 'is' behave unexpectedly sometimes."),
        QuestionModel(exam_id=mock.id, question_text="What is the correct description of a decorator?", option_a="A function that only works with classes", option_b="A wrapper that modifies or extends the behavior of a function or class without changing its source code", option_c="A built-in Python keyword for adding metadata", option_d="A method that runs before __init__", correct_option="B", explanation="Decorators aren't a keyword — they're just functions that take functions as arguments."),
        QuestionModel(exam_id=mock.id, question_text="Why are generators more memory-efficient than lists?", option_a="Generators store all values in a compressed format", option_b="Generators use C extensions internally for speed", option_c="Generators produce values one at a time on demand, not all at once", option_d="Generators skip duplicate values automatically", correct_option="C", explanation="Students often think it's about compression or C speed rather than lazy evaluation."),
        QuestionModel(exam_id=mock.id, question_text="Which statement about list comprehensions vs loops is TRUE?", option_a="List comprehensions can always replace loops", option_b="Loops are always faster than list comprehensions", option_c="List comprehensions are concise for creating lists; loops are more general-purpose", option_d="List comprehensions support break and continue", correct_option="C", explanation="List comprehensions cannot handle all loop patterns (e.g., complex branching, multiple side effects)."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following is NOT a built-in Python data structure?", option_a="List", option_b="Dictionary", option_c="Stack", option_d="Set", correct_option="C", explanation="Stack is a concept/pattern — Python doesn't have a built-in stack type (you use a list or deque)."),
        QuestionModel(exam_id=mock.id, question_text="The Global Interpreter Lock (GIL) means:", option_a="Python cannot run on multiple CPU cores at all", option_b="Only one thread executes Python bytecode at a time, even on multi-core systems", option_c="Global variables are locked from modification during execution", option_d="Only one Python script can run on a machine at a time", correct_option="B", explanation="The GIL doesn't mean no concurrency — I/O-bound threads still benefit. It only limits CPU-bound threads."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following is IMMUTABLE in Python?", option_a="List", option_b="Dictionary", option_c="Set", option_d="Tuple", correct_option="D", explanation="All four are built-in structures, but only tuple is immutable. Sets feel 'fixed' to students but are actually mutable."),
        QuestionModel(exam_id=mock.id, question_text="What happens if an exception is NOT handled in Python?", option_a="Python silently ignores it and continues", option_b="Python automatically retries the operation", option_c="The program terminates and prints a traceback", option_d="Python converts it to a warning", correct_option="C", explanation="Students sometimes think Python handles it gracefully by default."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following best describes a namespace in Python?", option_a="A folder where Python modules are stored", option_b="A mapping of names to objects that isolates identifiers within a program", option_c="A reserved block of memory for global variables", option_d="A special file that lists all variables in a module", correct_option="B", explanation="Students confuse namespace with file system directories or __init__.py."),
        QuestionModel(exam_id=mock.id, question_text="What is the difference between a module and a package in Python?", option_a="A module is a class; a package is a module", option_b="A module is a single .py file of reusable code; a package is a directory of modules", option_c="There is no difference; the terms are interchangeable", option_d="A package is a compiled module", correct_option="B", explanation="The distinction is .py file vs directory with __init__.py."),
        QuestionModel(exam_id=mock.id, question_text="When does the if __name__ == __main__: block execute?", option_a="Every time the file is imported", option_b="Only when the file is run directly, not when imported", option_c="Only when called explicitly from another module", option_d="When the module is compiled for the first time", correct_option="B", explanation="Students confuse imported and run directly behavior."),
        QuestionModel(exam_id=mock.id, question_text="What distinguishes a method from a function in Python?", option_a="Methods can return values; functions cannot", option_b="Functions are faster than methods", option_c="Methods are defined inside a class; functions are standalone", option_d="Methods must always take self as the last parameter", correct_option="C", explanation="self is the first parameter, not last. Also, static methods don't take self — but they're still methods."),
        QuestionModel(exam_id=mock.id, question_text="What will def fn(*args, **kwargs) allow?", option_a="Only positional arguments", option_b="Only keyword arguments", option_c="Any number of positional AND keyword arguments", option_d="Exactly one positional and one keyword argument", correct_option="C", explanation="Either alone is partial — the combo accepts both freely."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following is NOT a built-in data type in Python?", option_a="Boolean", option_b="Float", option_c="Array", option_d="Tuple", correct_option="C", explanation="array requires importing the array module — it's not a built-in type like list or tuple."),
        QuestionModel(exam_id=mock.id, question_text="In Python inheritance, what does the child class automatically get?", option_a="Only the parent's class variables, not its methods", option_b="A copy of the parent's code that it must redefine", option_c="Access to the parent's properties and methods, which it can also override", option_d="The parent's __init__ is always skipped", correct_option="C", explanation="Students think __init__ of parent is automatically called — it isn't unless super().__init__() is explicitly called."),
        QuestionModel(exam_id=mock.id, question_text="When would you use __repr__ over __str__?", option_a="When you want output displayed to end users", option_b="When you want a developer-friendly, unambiguous representation of the object", option_c="__repr__ is only used in Python 2", option_d="When the object has no string conversion", correct_option="B", explanation="__str__ = user-facing; __repr__ = developer-facing. Many students swap them."),
        QuestionModel(exam_id=mock.id, question_text="Which statement about Python 3 vs Python 2 is TRUE?", option_a='print "hello" works in both Python 2 and 3', option_b="Python 3 made print a function, removing the statement syntax", option_c="Python 3 introduced the print keyword", option_d="print() is only available after importing sys", correct_option="B", explanation="print as a keyword vs function is the classic Python 2/3 trap."),
        QuestionModel(exam_id=mock.id, question_text="What happens when an assert statement fails?", option_a="It prints a warning and continues execution", option_b="It logs the error silently", option_c="It raises an AssertionError", option_d="It raises a ValueError", correct_option="C", explanation="Students often guess ValueError since the value is wrong — but it's always AssertionError."),
        QuestionModel(exam_id=mock.id, question_text="What is the key difference between yield and return?", option_a="yield can only be used inside a class", option_b="return pauses the function; yield terminates it", option_c="yield pauses the function and remembers its state; return terminates it", option_d="Both do the same thing but yield is used for generators only as a style choice", correct_option="C", explanation="Option B reverses the definitions — a very common student mistake."),
        QuestionModel(exam_id=mock.id, question_text="What problem do virtual environments primarily solve?", option_a="They speed up Python code execution", option_b="They allow Python 2 and Python 3 to run simultaneously", option_c="They isolate project dependencies to avoid version conflicts between projects", option_d="They encrypt your project's source code", correct_option="C", explanation="Students often think venvs are about speed or Python version switching."),
        QuestionModel(exam_id=mock.id, question_text="What is the difference between a shallow copy and a deep copy?", option_a="Shallow copies duplicate everything; deep copies only copy the reference", option_b="Shallow copies create a new object but share references to nested objects; deep copies create fully independent copies", option_c="There is no difference for immutable objects like strings and integers", option_d="Deep copies are only available via the copy module's copy() function", correct_option="B", explanation="Option A reverses the definitions. Option D: copy() is shallow — deepcopy() is deep."),
        QuestionModel(exam_id=mock.id, question_text="Where should a docstring be placed in a function?", option_a="After the last line of the function", option_b="As a comment using # before the function", option_c="As the first statement inside the function, using triple quotes", option_d="In a separate .doc file linked to the function", correct_option="C", explanation="Students confuse # comments with docstrings. Docstrings are string literals, not comments."),
        QuestionModel(exam_id=mock.id, question_text="What do metaclasses control in Python?", option_a="The behavior of instances of a class", option_b="The memory allocation of objects", option_c="The creation and behavior of classes themselves", option_d="The execution order of methods in a class", correct_option="C", explanation="Metaclasses operate one level above — they define how classes are made, not how instances behave."),
        # --- From python_mcq_mock_test.html (25 questions) ---
        QuestionModel(exam_id=mock.id, question_text="What does a lambda function return if no expression is provided after the colon?", option_a="None", option_b="0", option_c="An empty string", option_d="A SyntaxError is raised", correct_option="D", explanation="A lambda must have an expression — writing `lambda x:` with nothing after is a SyntaxError."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following is TRUE about closures in Python?", option_a="The enclosing function must be a class method", option_b="A closure captures variables by value at definition time", option_c="A closure can access and modify enclosing scope variables via nonlocal", option_d="Closures are only possible with lambda functions", correct_option="C", explanation="`nonlocal` lets an inner function modify a variable from the enclosing function's scope. Closures capture by reference, not by value."),
        QuestionModel(exam_id=mock.id, question_text="What is the key difference between @staticmethod and @classmethod?", option_a="@staticmethod receives cls, @classmethod receives self", option_b="@classmethod receives cls as first arg; @staticmethod receives no implicit arg", option_c="They are identical — just different naming conventions", option_d="@staticmethod can only be called on instances", correct_option="B", explanation="`@classmethod` gets the class (`cls`) as its first argument. `@staticmethod` gets no implicit first argument at all."),
        QuestionModel(exam_id=mock.id, question_text="What happens when __exit__ returns True in a context manager?", option_a="The with block runs again", option_b="The exception is suppressed", option_c="The program exits", option_d="Python raises a RuntimeError", correct_option="B", explanation="Returning `True` from `__exit__` tells Python to suppress the exception that triggered it."),
        QuestionModel(exam_id=mock.id, question_text="In Python's MRO (C3 linearization), if class C(A, B) and both A and B define method foo(), which is called?", option_a="B.foo() because it is listed last", option_b="A.foo() because it is listed first", option_c="Both are called automatically", option_d="Python raises an AttributeError", correct_option="B", explanation="MRO searches left to right: C → A → B → object. The first match (A.foo) wins."),
        QuestionModel(exam_id=mock.id, question_text="Which statement about Python's copy.deepcopy() is FALSE?", option_a="It creates a fully independent copy", option_b="It handles circular references", option_c="It is always faster than copy.copy()", option_d="It recursively copies nested objects", correct_option="C", explanation="`deepcopy` is generally slower than `copy` because it recurses into every nested object."),
        QuestionModel(exam_id=mock.id, question_text="What is the primary purpose of __slots__ in a Python class?", option_a="To prevent subclassing", option_b="To reduce memory usage by avoiding a per-instance __dict__", option_c="To make attributes read-only", option_d="To enable pickling of instances", correct_option="B", explanation="`__slots__` replaces the per-instance `__dict__` with a fixed-size array, saving memory when many instances exist."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following is an iterable but NOT an iterator?", option_a="A generator object", option_b="A file object", option_c="A list", option_d="An object returned by zip()", correct_option="C", explanation="A `list` is iterable (you can call `iter()` on it) but is not itself an iterator — it doesn't have `__next__`."),
        QuestionModel(exam_id=mock.id, question_text="What does the @property decorator primarily allow you to do?", option_a="Cache method return values automatically", option_b="Access a method like an attribute without parentheses", option_c="Prevent a method from being overridden", option_d="Convert a method into a static method", correct_option="B", explanation="`@property` lets you call `obj.value` instead of `obj.value()`, while still executing getter/setter/deleter logic."),
        QuestionModel(exam_id=mock.id, question_text="Monkey patching is best described as:", option_a="Optimizing code at compile time", option_b="Dynamically modifying a class or module at runtime", option_c="Patching security vulnerabilities in dependencies", option_d="A technique specific to test-driven development only", correct_option="B", explanation="Monkey patching replaces or adds attributes/methods on existing classes or modules at runtime."),
        QuestionModel(exam_id=mock.id, question_text="Given a = 'hello' and b = 'hello', which statement is always guaranteed to be True?", option_a="a is b", option_b="a == b", option_c="id(a) == id(b)", option_d="type(a) is not type(b)", correct_option="B", explanation="`==` compares values and is always True for equal strings. `is` checks identity — string interning is an implementation detail, not guaranteed."),
        QuestionModel(exam_id=mock.id, question_text="Which f-string expression correctly formats the float 3.14159 to 2 decimal places?", option_a='f"{3.14159:.2}"', option_b='f"{3.14159:2f}"', option_c='f"{3.14159:.2f}"', option_d='f"{3.14159|2f}"', correct_option="C", explanation="The format spec `:.2f` means fixed-point with 2 decimal places."),
        QuestionModel(exam_id=mock.id, question_text="What is __new__ responsible for that __init__ is not?", option_a="Setting instance attributes", option_b="Allocating and returning the new object instance", option_c="Calling the parent class constructor", option_d="Registering the class in a metaclass", correct_option="B", explanation="`__new__` creates and returns the instance. `__init__` then initialises its attributes."),
        QuestionModel(exam_id=mock.id, question_text="What problem can arise with multiple inheritance in Python if not handled carefully?", option_a="The GIL blocks all threads", option_b="The Diamond Problem — ambiguous method resolution", option_c="Memory leaks from circular references", option_d="Metaclass conflicts with ABCs", correct_option="B", explanation="The Diamond Problem occurs when two parent classes share a common ancestor, making method resolution ambiguous without MRO."),
        QuestionModel(exam_id=mock.id, question_text="In async/await, what does await do exactly?", option_a="Blocks the entire process until the coroutine completes", option_b="Suspends the current coroutine, yielding control to the event loop", option_c="Starts a new OS thread", option_d="Converts a regular function into a coroutine", correct_option="B", explanation="`await` pauses the current coroutine and yields control back to the event loop."),
        QuestionModel(exam_id=mock.id, question_text="What is the difference between __getattr__ and __getattribute__?", option_a="__getattr__ is called for every attribute access; __getattribute__ only for missing ones", option_b="__getattribute__ is called for every attribute access; __getattr__ only when normal lookup fails", option_c="They are identical — Python uses whichever is defined first", option_d="__getattr__ only works on class attributes", correct_option="B", explanation="`__getattribute__` intercepts ALL attribute access. `__getattr__` is a fallback only when the attribute isn't found."),
        QuestionModel(exam_id=mock.id, question_text="Why doesn't using multiple threads in Python always speed up CPU-bound tasks?", option_a="Python threads don't share memory", option_b="The GIL allows only one thread to run Python bytecode at a time", option_c="Python threads have higher startup overhead than processes", option_d="asyncio is required for true parallelism", correct_option="B", explanation="The GIL prevents true parallel execution. CPU-bound work should use multiprocessing to bypass it."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following correctly defines an abstract method in Python?", option_a="def method(self): raise NotImplementedError", option_b="from abc import ABC, abstractmethod — then @abstractmethod on the method", option_c="Prefixing the method name with double underscore", option_d="Decorating with @staticmethod inside an ABC", correct_option="B", explanation="The official way is `from abc import ABC, abstractmethod` and applying `@abstractmethod`."),
        QuestionModel(exam_id=mock.id, question_text="What does functools.reduce(lambda x, y: x*y, [1,2,3,4]) return?", option_a="10", option_b="24", option_c="None", option_d="A reduce object", correct_option="B", explanation="`reduce` applies the function cumulatively: ((1×2)×3)×4 = 24."),
        QuestionModel(exam_id=mock.id, question_text="What does functools.lru_cache do when the cache is full?", option_a="Raises a MemoryError", option_b="Discards the least recently used entry", option_c="Clears the entire cache", option_d="Stores new entries on disk", correct_option="B", explanation="LRU (Least Recently Used) cache evicts the entry accessed least recently to make room for new entries."),
        QuestionModel(exam_id=mock.id, question_text="What is the key distinction between a module and a package?", option_a="A module can contain classes; a package cannot", option_b="A package is a directory with an __init__.py; a module is a single .py file", option_c="Modules are imported; packages are installed", option_d="There is no difference — the terms are interchangeable", correct_option="B", explanation="A module is a `.py` file. A package is a directory containing an `__init__.py`."),
        QuestionModel(exam_id=mock.id, question_text="Which of the following is automatically provided by @dataclass that a regular class lacks?", option_a="Inheritance support", option_b="__repr__, __eq__, and optionally __init__ generated automatically", option_c="Thread safety on attribute access", option_d="Lazy property evaluation", correct_option="B", explanation="`@dataclass` auto-generates `__init__`, `__repr__`, and `__eq__` based on annotated fields, reducing boilerplate."),
        QuestionModel(exam_id=mock.id, question_text="What does zip([1,2,3], [4,5]) produce?", option_a="[(1,4),(2,5),(3,None)]", option_b="[(1,4),(2,5)]", option_c="[(1,4),(2,5),(3,)]", option_d="A ValueError — lists are different lengths", correct_option="B", explanation="`zip` stops at the shortest iterable, so only two pairs are produced."),
        QuestionModel(exam_id=mock.id, question_text="What is the output of list(enumerate(['a','b','c'], start=1))?", option_a="[(0,'a'),(1,'b'),(2,'c')]", option_b="[(1,'a'),(2,'b'),(3,'c')]", option_c="[('a',1),('b',2),('c',3)]", option_d="['1a','2b','3c']", correct_option="B", explanation="`start=1` shifts the counter to begin at 1, giving (1,'a'), (2,'b'), (3,'c')."),
        QuestionModel(exam_id=mock.id, question_text="A metaclass in Python is best described as:", option_a="A class that cannot be instantiated", option_b="A class whose instances are themselves classes", option_c="A class with only static methods", option_d="A subclass of type that adds encryption", correct_option="B", explanation="A metaclass is a class of a class. When you define a class, Python uses a metaclass (by default `type`) to create it."),
    ]
    db.add_all(mock_questions)
    db.commit()
    print("Database seeding completed. Certification exam + Python Mock Test created.")

# Schema/table DDL and migrations are intentionally NOT executed from application code.
# Run them directly in Oracle as DBA-controlled scripts.

# Dependency to get db session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ================================================================
# Pydantic Schemas (UNCHANGED)
# ================================================================
class LoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str

class ExamCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    duration_minutes: int
    created_by: int
    exam_type: str = "CERTIFICATION"  # CERTIFICATION or MOCK_TEST

class ExamResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    duration_minutes: int
    created_by: Optional[int]
    exam_type: str = "CERTIFICATION"

    model_config = ConfigDict(from_attributes=True)

class QuestionCreate(BaseModel):
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_option: str
    explanation: Optional[str] = None  # shown after answer in MOCK_TEST mode

class QuestionResponse(BaseModel):
    id: int
    exam_id: int
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_option: str
    explanation: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

class SaveAnswerItem(BaseModel):
    question_id: int
    selected_option: Optional[str] = None

class SaveAnswersRequest(BaseModel):
    answers: List[SaveAnswerItem]

class ViolationCreate(BaseModel):
    violation_type: str
    description: str

class StartAttemptRequest(BaseModel):
    user_id: int
    exam_id: int
    is_custom_mock: Optional[bool] = False

# ========== NEW SCHEMAS FOR QUESTION BANK & MOCK TESTS ==========
# Add these after the existing schemas (around line 520)

class QuestionBankCreate(BaseModel):
    category: str
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_option: str
    explanation: Optional[str] = None

class QuestionBankResponse(BaseModel):
    id: int
    category: str
    question_text: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_option: str
    explanation: Optional[str] = None
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)

class MockTestCreate(BaseModel):
    title: str
    category: str
    description: Optional[str] = ""
    duration_minutes: int
    scheduled_date: str  # YYYY-MM-DD format
    start_time: str      # HH:MM format
    end_time: str        # HH:MM format
    question_ids: List[int]  # IDs from question_bank

class MockTestResponse(BaseModel):
    id: int
    title: str
    category: str
    description: Optional[str]
    duration_minutes: int
    scheduled_date: str
    start_time: str
    end_time: str
    is_published: int
    total_questions: Optional[int] = 0
    questions_with_numbers: Optional[List[dict]] = None
    
    model_config = ConfigDict(from_attributes=True)

class BulkQuestionUploadResponse(BaseModel):
    total_rows: int
    valid_rows: int
    invalid_rows: List[int]
    errors: List[str]

class UpcomingTestResponse(BaseModel):
    id: int
    title: str
    category: str
    description: Optional[str]
    duration_minutes: int
    scheduled_date: str
    start_time: str
    end_time: str
    attempts_used: Optional[int] = 0
    remaining_attempts: Optional[int] = 3
    has_in_progress: Optional[bool] = False
    can_attempt: Optional[bool] = True
    is_upcoming: Optional[bool] = False
    is_active: Optional[bool] = False
    starts_at: Optional[str] = ""
    expires_at: Optional[str] = ""

class PreviousAttemptResponse(BaseModel):
    id: int
    test_title: str
    score: int
    total_questions: int
    percentage: float
    attempt_date: str


# -------------------------------------------------------------
# SMTP Mail Worker Function (UNCHANGED)
# -------------------------------------------------------------
def send_ses_email_worker(attempt_id: int, candidate_name: str, candidate_email: str, user_id: int, exam_title: str, total_q: int, answered_q: int, correct_q: int, wrong_q: int, percentage: float, status_str: str, violations: dict, qa_list: list, recording_url: str):
    """
    Sends detailed exam scorecard to admin using AWS SES, falling back to SMTP or local dump if SES credentials are not set.
    """
    print(f"[Scorecard Email Thread] Scorecard generation started for attempt {attempt_id}...")
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_REGION", "eu-north-1")

    # FIX Bug #7: Validate SES email addresses. Missing/unverified addresses cause silent
    # MessageRejected failures in SES sandbox. Log clearly and skip SES if not configured.
    sender_email = os.getenv("SES_SENDER_EMAIL")
    recipient_email = os.getenv("ADMIN_EMAIL")

    if not sender_email:
        print("[Scorecard Email Thread] WARNING: SES_SENDER_EMAIL env variable is not set. "
              "Skipping SES — falling through to SMTP/local dump.")
        aws_access_key = None  # Force-skip the SES block below

    if not recipient_email:
        print("[Scorecard Email Thread] WARNING: ADMIN_EMAIL env variable is not set. "
              "Defaulting recipient to sender address.")
        recipient_email = sender_email or "admin@example.com"
    
    subject = f"Exam Result Scorecard - {candidate_name}"
    
    # 1. Generate HTML Content (HTML)
    qa_rows_html = ""
    for idx, item in enumerate(qa_list):
        status_color = "#2ecc71" if item["result"] == "CORRECT" else "#e74c3c"
        selected_disp = item["selected"] if item["selected"] else "None"
        qa_rows_html += f"""
        <tr style="border-bottom: 1px solid #eee;">
            <td style="padding: 10px; border: 1px solid #ddd;">Q{idx+1}: {item["text"][:100]}...</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: center; font-weight: bold;">{selected_disp}</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: center; font-weight: bold;">{item["correct"]}</td>
            <td style="padding: 10px; border: 1px solid #ddd; text-align: center; color: {status_color}; font-weight: bold;">{item["result"]}</td>
        </tr>
        """
        
    body_html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; background-color: #f4f6f9; padding: 20px;">
        <div style="max-width: 700px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; border: 1px solid #ddd; box-shadow: 0 4px 10px rgba(0,0,0,0.05);">
            <h2 style="color: #4A90E2; border-bottom: 2px solid #4A90E2; padding-bottom: 10px; margin-top: 0; text-align: center;">Exam Completion Scorecard</h2>
            
            <h3 style="color: #333; margin-top: 20px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Candidate Information</h3>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr><td style="padding: 6px; font-weight: bold; width: 35%;">Candidate Name:</td><td style="padding: 6px;">{candidate_name}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Candidate Email:</td><td style="padding: 6px;">{candidate_email}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">User ID:</td><td style="padding: 6px;">{user_id}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Attempt ID:</td><td style="padding: 6px;">{attempt_id}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Exam Name:</td><td style="padding: 6px;">{exam_title}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Submission Date:</td><td style="padding: 6px;">{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>
            </table>

            <h3 style="color: #333; margin-top: 20px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Result Summary</h3>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr><td style="padding: 6px; font-weight: bold; width: 35%;">Total Questions:</td><td style="padding: 6px;">{total_q}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Answered Questions:</td><td style="padding: 6px;">{answered_q}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Correct Answers:</td><td style="padding: 6px;">{correct_q}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Wrong Answers:</td><td style="padding: 6px;">{wrong_q}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Score Percentage:</td><td style="padding: 6px; font-weight: bold; color: #3b82f6;">{percentage:.1f}%</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Pass/Fail Status:</td><td style="padding: 6px;"><span style="color: {'#2ecc71' if status_str == 'PASS' else '#e74c3c'}; font-weight: bold;">{status_str}</span></td></tr>
            </table>

            <h3 style="color: #333; margin-top: 20px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Proctoring Summary</h3>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr><td style="padding: 6px; font-weight: bold; width: 35%;">Tab Switches:</td><td style="padding: 6px; color: {'#e74c3c' if violations.get('tab_switch', 0) > 0 else '#333'};">{violations.get('tab_switch', 0)}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Fullscreen Exits:</td><td style="padding: 6px; color: {'#e74c3c' if violations.get('fullscreen_exit', 0) > 0 else '#333'};">{violations.get('fullscreen_exit', 0)}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Right Click Attempts:</td><td style="padding: 6px; color: {'#e74c3c' if violations.get('right_click', 0) > 0 else '#333'};">{violations.get('right_click', 0)}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Copy/Paste Attempts:</td><td style="padding: 6px; color: {'#e74c3c' if violations.get('copy_paste_attempt', 0) > 0 else '#333'};">{violations.get('copy_paste_attempt', 0)}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Webcam Denials:</td><td style="padding: 6px; color: {'#e74c3c' if violations.get('webcam_denied', 0) > 0 else '#333'};">{violations.get('webcam_denied', 0)}</td></tr>
                <tr><td style="padding: 6px; font-weight: bold;">Webcam Disconnects:</td><td style="padding: 6px; color: {'#e74c3c' if violations.get('webcam_disconnected', 0) > 0 else '#333'};">{violations.get('webcam_disconnected', 0)}</td></tr>
            </table>

            <h3 style="color: #333; margin-top: 20px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Question-wise Analysis</h3>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 0.9em;">
                <thead>
                    <tr style="background-color: #f2f2f2;">
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: left;">Question</th>
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: center; width: 15%;">Selected</th>
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: center; width: 15%;">Correct</th>
                        <th style="padding: 8px; border: 1px solid #ddd; text-align: center; width: 15%;">Result</th>
                    </tr>
                </thead>
                <tbody>
                    {qa_rows_html}
                </tbody>
            </table>

            <h3 style="color: #333; margin-top: 20px; border-bottom: 1px solid #eee; padding-bottom: 5px;">Session Recording Reference</h3>
            <p style="margin-top: 10px;">
                <strong>Webcam Recording Link:</strong><br>
                <a href="{recording_url}" style="color: #3b82f6; text-decoration: none;" target="_blank">{recording_url}</a>
            </p>
            
            <hr style="border: none; border-top: 1px solid #eee; margin-top: 30px;">
            <p style="font-size: 11px; color: #888; text-align: center; margin-bottom: 0;">Automated notification from Online Proctored Exam System.</p>
        </div>
    </body>
    </html>
    """

    print(f"[Scorecard Email Thread] Scorecard generated successfully for attempt {attempt_id}.")

    # 2. Try AWS SES
    if aws_access_key and aws_secret_key:
        print("[Scorecard Email Thread] Attempting to send scorecard via AWS SES...")
        print(f"[Scorecard Email Thread] SES Region: {aws_region}")
        print(f"[Scorecard Email Thread] SES From: {sender_email}  To: {recipient_email}")
        try:
            import boto3
            from botocore.exceptions import ClientError as BotoClientError
            ses_client = boto3.client(
                'ses',
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                region_name=aws_region
            )
            response = ses_client.send_email(
                Destination={'ToAddresses': [recipient_email]},
                Message={
                    'Body': {'Html': {'Charset': 'UTF-8', 'Data': body_html}},
                    'Subject': {'Charset': 'UTF-8', 'Data': subject}
                },
                Source=sender_email
            )
            print(f"[Scorecard Email Thread] SES Email sent successfully. Message ID: {response['MessageId']}")
            return
        except BotoClientError as e:
            # ROOT CAUSE FIX 5: Expose the exact SES error code.
            # Common codes: MessageRejected (address not verified in sandbox),
            # SignatureDoesNotMatch (wrong secret key), AccessDenied (IAM missing ses:SendEmail),
            # IdentityNotVerified (sender not verified in SES console).
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            print(f"[Scorecard Email Thread] SES FAILED — [{error_code}]: {error_msg}")
            print(f"[Scorecard Email Thread] Falling back to SMTP...")
        except Exception as e:
            print(f"[Scorecard Email Thread] SES FAILED — Unexpected [{type(e).__name__}]: {str(e)}")
            print(f"[Scorecard Email Thread] Falling back to SMTP...")

    # 3. Try standard SMTP configuration
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = os.getenv("SMTP_PORT", "587")
    smtp_sender = os.getenv("SMTP_SENDER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if all([smtp_server, smtp_sender, smtp_password]):
        print("[Scorecard Email Thread] Attempting to send detailed scorecard via SMTP...")
        try:
            msg = MIMEMultipart()
            msg['From'] = smtp_sender
            msg['To'] = recipient_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body_html, 'html'))
            
            server = smtplib.SMTP(smtp_server, int(smtp_port), timeout=10)
            server.starttls()
            server.login(smtp_sender, smtp_password)
            server.send_message(msg)
            server.quit()
            print(f"[Scorecard Email Thread] SMTP Email sent successfully to {recipient_email}.")
            return
        except Exception as smtp_err:
            print(f"[Scorecard Email Thread] SMTP Email failed: {str(smtp_err)}")
    else:
        print("[Scorecard Email Thread] SMTP configuration missing. Cannot send via SMTP.")

    # 4. Fallback: Dump scorecard locally
    print("[Scorecard Email Thread] Dumping detailed scorecard locally as fallback...")
    try:
        os.makedirs("static/scorecards", exist_ok=True)
        local_path = f"static/scorecards/scorecard_attempt_{attempt_id}.html"
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(body_html)
        print(f"[Scorecard Email Thread] Scorecard dumped locally at {local_path}")
    except Exception as dump_err:
        print(f"[Scorecard Email Thread] ERROR: Failed to dump local scorecard: {str(dump_err)}")

# -------------------------------------------------------------
# API Endpoints (ALL UNCHANGED)
# -------------------------------------------------------------

# Auth
@app.post("/api/login", response_model=UserResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    hashed_pwd = hashlib.sha256(payload.password.encode()).hexdigest()
    user = db.query(UserModel).filter(UserModel.username == payload.username).first()
    if not user or user.password != hashed_pwd:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return UserResponse(id=user.id, username=user.username, email=user.email, role=user.role)

# Exams CRUD
@app.post("/api/exams", response_model=ExamResponse)
def create_exam(exam: ExamCreate, db: Session = Depends(get_db)):
    db_exam = ExamModel(
        title=exam.title,
        description=exam.description,
        duration_minutes=exam.duration_minutes,
        created_by=exam.created_by,
        exam_type=exam.exam_type
    )
    db.add(db_exam)
    db.commit()
    db.refresh(db_exam)
    return db_exam

@app.get("/api/exams", response_model=List[ExamResponse])
def get_all_exams(db: Session = Depends(get_db)):
    return db.query(ExamModel).all()

@app.get("/api/exams/{exam_id}", response_model=ExamResponse)
def get_exam(exam_id: int, db: Session = Depends(get_db)):
    exam = db.query(ExamModel).filter(ExamModel.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    return exam

@app.put("/api/exams/{exam_id}", response_model=ExamResponse)
def update_exam(exam_id: int, payload: ExamCreate, db: Session = Depends(get_db)):
    exam = db.query(ExamModel).filter(ExamModel.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    exam.title = payload.title
    exam.description = payload.description
    exam.duration_minutes = payload.duration_minutes
    exam.exam_type = payload.exam_type
    db.commit()
    db.refresh(exam)
    return exam

@app.delete("/api/exams/{exam_id}")
def delete_exam(exam_id: int, db: Session = Depends(get_db)):
    exam = db.query(ExamModel).filter(ExamModel.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    # Delete related questions and attempts first to handle FK dependency
    db.query(QuestionModel).filter(QuestionModel.exam_id == exam_id).delete(synchronize_session=False)
    db.query(ExamAttemptModel).filter(ExamAttemptModel.exam_id == exam_id).delete(synchronize_session=False)
    db.query(ResultModel).filter(ResultModel.exam_id == exam_id).delete(synchronize_session=False)
    db.delete(exam)
    db.commit()
    return {"message": "Exam deleted successfully"}

# Questions CRUD
@app.post("/api/exams/{exam_id}/questions", response_model=QuestionResponse)
def add_question(exam_id: int, payload: QuestionCreate, db: Session = Depends(get_db)):
    exam = db.query(ExamModel).filter(ExamModel.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    db_question = QuestionModel(
        exam_id=exam_id,
        question_text=payload.question_text,
        option_a=payload.option_a,
        option_b=payload.option_b,
        option_c=payload.option_c,
        option_d=payload.option_d,
        correct_option=payload.correct_option,
        explanation=payload.explanation
    )
    db.add(db_question)
    db.commit()
    db.refresh(db_question)
    return db_question

@app.get("/api/exams/{exam_id}/questions", response_model=List[QuestionResponse])
def get_exam_questions(exam_id: int, db: Session = Depends(get_db)):
    exam = db.query(ExamModel).filter(ExamModel.id == exam_id).first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    return db.query(QuestionModel).filter(QuestionModel.exam_id == exam_id).all()

# ================================================================
# FIX: Exam Attempt control with proper time checks
# ================================================================
@app.post("/api/attempts/start")
def start_exam_attempt(payload: StartAttemptRequest, db: Session = Depends(get_db)):
    if payload.is_custom_mock:
        mock_test = db.query(MockTestModel).filter(MockTestModel.id == payload.exam_id).first()
        if not mock_test:
            raise HTTPException(status_code=404, detail="Mock test not found")
        
        # Check if test is published
        if mock_test.is_published != 1:
            raise HTTPException(status_code=403, detail="This mock test is not published yet.")
        
        # ================================================================
        # TIME CHECKS: Start time, 24-hour window
        # ================================================================
        from datetime import datetime, timedelta
        
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        current_time_str = now.strftime("%H:%M")
        
        # 1. Check if test is scheduled for today
        if mock_test.scheduled_date != today_str:
            raise HTTPException(
                status_code=403,
                detail=f"This test is scheduled for {mock_test.scheduled_date}. Please wait."
            )
        
        # 2. Check if current time is before start time (disable before start)
        if current_time_str < mock_test.start_time:
            raise HTTPException(
                status_code=403,
                detail=f"This test starts at {mock_test.start_time}. Please wait."
            )
        
        # 3. Check if test is still within 24-hour window from start time
        scheduled_datetime = datetime.strptime(
            f"{mock_test.scheduled_date} {mock_test.start_time}",
            "%Y-%m-%d %H:%M"
        )
        test_end_time = scheduled_datetime + timedelta(hours=24)
        
        if now > test_end_time:
            raise HTTPException(
                status_code=403,
                detail="This test is no longer available (24-hour window expired)."
            )
        
        # Check if there's an in-progress attempt (allow resume)
        in_progress = db.query(UserTestAttemptModel).filter(
            UserTestAttemptModel.user_id == payload.user_id,
            UserTestAttemptModel.mock_test_id == payload.exam_id,
            UserTestAttemptModel.status.in_(["in_progress", "started"])
        ).first()

        # ================================================================
        # ATTEMPT COUNT: Count ALL attempts (not just completed)
        # ================================================================
        total_attempts = db.query(UserTestAttemptModel).filter(
            UserTestAttemptModel.user_id == payload.user_id,
            UserTestAttemptModel.mock_test_id == payload.exam_id
        ).count()
        
        if in_progress:
            # Resume existing attempt
            return {
                "attempt_id": in_progress.id,
                "duration_minutes": mock_test.duration_minutes,
                "exam_type": "MOCK_TEST",
                "attempt_number": total_attempts + 1,
                "remaining_attempts": MAX_ATTEMPTS_PER_TEST - total_attempts,
                "can_start": True
            }

        if total_attempts >= MAX_ATTEMPTS_PER_TEST:
            raise HTTPException(
                status_code=403,
                detail=f"You have already used all {MAX_ATTEMPTS_PER_TEST} attempts for this test."
            )
        
        # Create new attempt
        attempt = UserTestAttemptModel(
            user_id=payload.user_id,
            mock_test_id=payload.exam_id,
            status="in_progress"
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
        
        return {
            "attempt_id": attempt.id,
            "duration_minutes": mock_test.duration_minutes,
            "exam_type": "MOCK_TEST",
            "attempt_number": total_attempts + 1,
            "remaining_attempts": MAX_ATTEMPTS_PER_TEST - (total_attempts + 1),
            "can_start": True
        }
    else:
        # Check if exam exists (CERTIFICATION - unchanged)
        exam = db.query(ExamModel).filter(ExamModel.id == payload.exam_id).first()
        if not exam:
            raise HTTPException(status_code=404, detail="Exam not found")
        
        # Create new attempt
        attempt = ExamAttemptModel(
            user_id=payload.user_id,
            exam_id=payload.exam_id,
            status="started"
        )
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
        return {
            "attempt_id": attempt.id,
            "duration_minutes": exam.duration_minutes,
            "exam_type": exam.exam_type
        }

# Save Answers
@app.post("/api/attempts/{attempt_id}/answers")
def save_answers(attempt_id: int, payload: SaveAnswersRequest, db: Session = Depends(get_db)):
    attempt = db.query(ExamAttemptModel).filter(ExamAttemptModel.id == attempt_id).first()
    if attempt:
        # Delete previous answers for this attempt
        db.query(AnswerModel).filter(AnswerModel.attempt_id == attempt_id).delete()
        
        # Write new answers
        for ans in payload.answers:
            db_answer = AnswerModel(
                attempt_id=attempt_id,
                question_id=ans.question_id,
                selected_option=ans.selected_option
            )
            db.add(db_answer)
        
        db.commit()
        return {"message": "Answers saved successfully"}
        
    mock_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
    if mock_attempt:
        import json
        ans_dict = {str(ans.question_id): ans.selected_option for ans in payload.answers}
        mock_attempt.answers = json.dumps(ans_dict)
        db.commit()
        return {"message": "Answers saved successfully"}
        
    raise HTTPException(status_code=404, detail="Exam attempt not found")

# Violations Logging
@app.post("/api/attempts/{attempt_id}/violations")
@app.post("/api/attempts/{attempt_id}/violations")
def log_violation(attempt_id: int, payload: ViolationCreate, db: Session = Depends(get_db)):
    attempt = db.query(ExamAttemptModel).filter(ExamAttemptModel.id == attempt_id).first()
    if attempt:
        violation = ProctorLogModel(
            attempt_id=attempt_id,
            violation_type=payload.violation_type,
            description=payload.description,
            is_mock=False
        )
        db.add(violation)
        db.commit()
        return {"message": "Violation logged successfully"}
        
    mock_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
    if mock_attempt:
        violation = ProctorLogModel(
            attempt_id=attempt_id,
            violation_type=payload.violation_type,
            description=payload.description,
            is_mock=True
        )
        db.add(violation)
        db.commit()
        return {"message": "Mock test attempt violation logged successfully"}
        
    raise HTTPException(status_code=404, detail="Exam attempt not found")

@app.get("/api/attempts/{attempt_id}/violations")
def get_attempt_violations(attempt_id: int, db: Session = Depends(get_db)):
    mock_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
    if mock_attempt:
        return db.query(ProctorLogModel).filter(ProctorLogModel.attempt_id == attempt_id, ProctorLogModel.is_mock == True).all()
    return db.query(ProctorLogModel).filter(ProctorLogModel.attempt_id == attempt_id, ProctorLogModel.is_mock == False).all()

# Exam Evaluation / Submit
@app.post("/api/attempts/{attempt_id}/submit")
def submit_exam(attempt_id: int, db: Session = Depends(get_db)):
    print(f"[API] Submitting and evaluating exam attempt {attempt_id}...")
    attempt = db.query(ExamAttemptModel).filter(ExamAttemptModel.id == attempt_id).first()
    if attempt:
        if attempt.status == "completed":
            # Already graded, get the existing result
            res = db.query(ResultModel).filter(ResultModel.attempt_id == attempt_id).first()
            if res:
                return {"result_id": res.id, "score": res.score, "percentage": res.percentage}
                
        # Mark attempt as completed
        attempt.status = "completed"
        attempt.completed_at = datetime.utcnow()
        
        # Evaluate score
        questions = db.query(QuestionModel).filter(QuestionModel.exam_id == attempt.exam_id).all()
        total_q = len(questions)
        
        # Map questions for fast access
        q_map = {q.id: q.correct_option for q in questions}
        
        # Fetch candidate answers
        answers = db.query(AnswerModel).filter(AnswerModel.attempt_id == attempt_id).all()
        correct_cnt = 0
        answered_cnt = 0
        
        ans_map = {}
        for ans in answers:
            if ans.selected_option is not None and ans.selected_option.strip() != "":
                answered_cnt += 1
                ans_map[ans.question_id] = ans.selected_option
                if ans.question_id in q_map and ans.selected_option == q_map[ans.question_id]:
                    correct_cnt += 1
            else:
                ans_map[ans.question_id] = None
                
        wrong_cnt = answered_cnt - correct_cnt
        percentage = (correct_cnt / total_q * 100.0) if total_q > 0 else 0.0
        status_str = "PASS" if percentage >= 50.0 else "FAIL"
        
        # Save results
        result = ResultModel(
            attempt_id=attempt_id,
            user_id=attempt.user_id,
            exam_id=attempt.exam_id,
            total_questions=total_q,
            correct_answers=correct_cnt,
            score=float(correct_cnt),
            percentage=percentage,
            status=status_str
        )
        db.add(result)
        db.commit()
        
        # Get detailed proctor logs and violations counts
        violations_list = db.query(ProctorLogModel).filter(ProctorLogModel.attempt_id == attempt_id).all()
        violations_summary = {
            "tab_switch": 0,
            "fullscreen_exit": 0,
            "right_click": 0,
            "copy_paste_attempt": 0,
            "webcam_denied": 0,
            "webcam_disconnected": 0
        }
        for log in violations_list:
            v_type = log.violation_type
            if v_type in violations_summary:
                violations_summary[v_type] += 1
                
        # Gather question-wise list
        qa_list = []
        for q in questions:
            selected = ans_map.get(q.id)
            is_correct = "CORRECT" if selected == q.correct_option else "WRONG"
            qa_list.append({
                "text": q.question_text,
                "selected": selected,
                "correct": q.correct_option,
                "result": is_correct
            })
            
        # Get Candidate User & Exam Info for Email
        candidate = db.query(UserModel).filter(UserModel.id == attempt.user_id).first()
        exam_obj = db.query(ExamModel).filter(ExamModel.id == attempt.exam_id).first()

        cand_name = candidate.username if candidate else "Unknown Candidate"
        cand_email = candidate.email if candidate else "admin@chakorahub.com"
        exam_title = exam_obj.title if exam_obj else "Unknown Exam"
        exam_type = exam_obj.exam_type if exam_obj else "CERTIFICATION"

        db.expire(attempt)  # force SQLAlchemy to re-query on next access
        fresh_attempt = db.query(ExamAttemptModel).filter(ExamAttemptModel.id == attempt_id).first()
        recording_url = fresh_attempt.recording_url if fresh_attempt and fresh_attempt.recording_url else "No recording uploaded"

        if exam_type == "CERTIFICATION":
            email_thread = threading.Thread(
                target=send_ses_email_worker,
                args=(
                    attempt_id,
                    cand_name,
                    cand_email,
                    attempt.user_id,
                    exam_title,
                    total_q,
                    answered_cnt,
                    correct_cnt,
                    wrong_cnt,
                    percentage,
                    status_str,
                    violations_summary,
                    qa_list,
                    recording_url
                ),
                daemon=True
            )
            email_thread.start()
            print(f"[API] SES scorecard thread started for CERTIFICATION attempt {attempt_id}. Recording URL: {recording_url}")
        else:
            print(f"[API] MOCK_TEST attempt {attempt_id} — skipping SES email (practice mode).")
        
        return {
            "result_id": result.id,
            "score": result.score,
            "percentage": result.percentage,
            "correct_answers": result.correct_answers,
            "total_questions": result.total_questions,
            "status": result.status
        }

    mock_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
    if mock_attempt:
        if mock_attempt.status == "completed":
            return {
                "result_id": mock_attempt.id,
                "score": mock_attempt.score,
                "percentage": mock_attempt.percentage,
                "correct_answers": mock_attempt.score,
                "total_questions": mock_attempt.total_questions,
                "status": "PASS" if mock_attempt.percentage >= 50.0 else "FAIL"
            }
            
        mock_attempt.status = "completed"
        
        # Load questions for this mock test
        test_questions = db.query(MockTestQuestionModel).filter(
            MockTestQuestionModel.mock_test_id == mock_attempt.mock_test_id
        ).all()
        total_q = len(test_questions)
        
        # Map correct options
        q_ids = [tq.question_id for tq in test_questions]
        questions = db.query(QuestionBankModel).filter(QuestionBankModel.id.in_(q_ids)).all()
        q_map = {q.id: q.correct_option for q in questions}
        
        # Parse saved answers from json
        import json
        ans_map = {}
        if mock_attempt.answers:
            try:
                ans_map = json.loads(mock_attempt.answers)
            except Exception:
                pass
                
        # Calculate score and prepare qa_list
        correct_cnt = 0
        answered_cnt = 0
        qa_list = []
        for q in questions:
            selected = ans_map.get(str(q.id)) or ans_map.get(q.id)
            if selected is not None and str(selected).strip() != "":
                answered_cnt += 1
                if selected == q.correct_option:
                    correct_cnt += 1
            is_correct = "CORRECT" if selected == q.correct_option else "WRONG"
            qa_list.append({
                "text": q.question_text,
                "selected": selected,
                "correct": q.correct_option,
                "result": is_correct
            })
            
        wrong_cnt = answered_cnt - correct_cnt
        percentage = (correct_cnt / total_q * 100.0) if total_q > 0 else 0.0
        status_str = "PASS" if percentage >= 50.0 else "FAIL"
        
        mock_attempt.score = correct_cnt
        mock_attempt.total_questions = total_q
        mock_attempt.percentage = percentage
        db.commit()

        # Send SES email scorecard
        candidate = db.query(UserModel).filter(UserModel.id == mock_attempt.user_id).first()
        test_obj = db.query(MockTestModel).filter(MockTestModel.id == mock_attempt.mock_test_id).first()

        cand_name = candidate.username if candidate else "Unknown Candidate"
        cand_email = candidate.email if candidate else "admin@chakorahub.com"
        exam_title = test_obj.title if test_obj else "Unknown Mock Test"

        violations_list = db.query(ProctorLogModel).filter(
            ProctorLogModel.attempt_id == attempt_id,
            ProctorLogModel.is_mock == True
        ).all()
        violations_summary = {
            "tab_switch": 0,
            "fullscreen_exit": 0,
            "right_click": 0,
            "copy_paste_attempt": 0,
            "webcam_denied": 0,
            "webcam_disconnected": 0
        }
        for log in violations_list:
            v_type = log.violation_type
            if v_type in violations_summary:
                violations_summary[v_type] += 1

        db.expire(mock_attempt)
        fresh_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
        recording_url = fresh_attempt.recording_url if fresh_attempt and fresh_attempt.recording_url else "No recording uploaded"

        email_thread = threading.Thread(
            target=send_ses_email_worker,
            args=(
                attempt_id,
                cand_name,
                cand_email,
                mock_attempt.user_id,
                exam_title,
                total_q,
                answered_cnt,
                correct_cnt,
                wrong_cnt,
                percentage,
                status_str,
                violations_summary,
                qa_list,
                recording_url
            ),
            daemon=True
        )
        email_thread.start()
        print(f"[API] SES scorecard thread started for MOCK_TEST attempt {attempt_id}. Recording URL: {recording_url}")
        
        return {
            "result_id": mock_attempt.id,
            "score": correct_cnt,
            "percentage": percentage,
            "correct_answers": correct_cnt,
            "total_questions": total_q,
            "status": "PASS" if percentage >= 50.0 else "FAIL"
        }
        
    raise HTTPException(status_code=404, detail="Exam attempt not found")

# View Candidate Results & Proctor Logs (Admin View)
@app.get("/api/results")
def get_all_results(db: Session = Depends(get_db)):
    results = db.query(ResultModel).all()
    mock_attempts = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.status == "completed").all()

    # Pre-fetch all related records in bulk queries to eliminate N+1.
    user_ids = {r.user_id for r in results}
    exam_ids = {r.exam_id for r in results}
    attempt_ids = [r.attempt_id for r in results]

    users_map = {
        u.id: u for u in db.query(UserModel).filter(UserModel.id.in_(user_ids)).all()
    }
    exams_map = {
        e.id: e for e in db.query(ExamModel).filter(ExamModel.id.in_(exam_ids)).all()
    }
    attempts_map = {
        a.id: a for a in db.query(ExamAttemptModel).filter(ExamAttemptModel.id.in_(attempt_ids)).all()
    }

    from sqlalchemy import func
    violations_map = {}
    if attempt_ids:
        violation_counts_raw = (
            db.query(ProctorLogModel.attempt_id, func.count(ProctorLogModel.id).label("cnt"))
            .filter(ProctorLogModel.attempt_id.in_(attempt_ids))
            .group_by(ProctorLogModel.attempt_id)
            .all()
        )
        violations_map = {row.attempt_id: row.cnt for row in violation_counts_raw}

    formatted = []
    
    # 1. Format certification attempts
    for r in results:
        user = users_map.get(r.user_id)
        exam = exams_map.get(r.exam_id)
        attempt = attempts_map.get(r.attempt_id)
        completed_str = "Unknown"
        if attempt and attempt.completed_at:
            from datetime import timezone
            utc_dt = attempt.completed_at.replace(tzinfo=timezone.utc)
            local_dt = utc_dt.astimezone()
            completed_str = local_dt.strftime("%Y-%m-%d %H:%M")
        formatted.append({
            "id": r.id,
            "attempt_id": r.attempt_id,
            "username": user.username if user else "Unknown",
            "exam_title": exam.title if exam else "Unknown",
            "score": r.score,
            "total_questions": r.total_questions,
            "percentage": r.percentage,
            "status": r.status,
            "violations_count": violations_map.get(r.attempt_id, 0),
            "completed_at": completed_str,
            "exam_type": "CERTIFICATION"
        })

    # 2. Format custom mock attempts
    if mock_attempts:
        mock_user_ids = {m.user_id for m in mock_attempts}
        mock_test_ids = {m.mock_test_id for m in mock_attempts}
        mock_attempt_ids = [m.id for m in mock_attempts]

        mock_users_map = {
            u.id: u for u in db.query(UserModel).filter(UserModel.id.in_(mock_user_ids)).all()
        }
        mock_tests_map = {
            t.id: t for t in db.query(MockTestModel).filter(MockTestModel.id.in_(mock_test_ids)).all()
        }

        from sqlalchemy import func
        mock_violations_map = {}
        if mock_attempt_ids:
            mock_violation_counts_raw = (
                db.query(ProctorLogModel.attempt_id, func.count(ProctorLogModel.id).label("cnt"))
                .filter(ProctorLogModel.attempt_id.in_(mock_attempt_ids), ProctorLogModel.is_mock == True)
                .group_by(ProctorLogModel.attempt_id)
                .all()
            )
            mock_violations_map = {row.attempt_id: row.cnt for row in mock_violation_counts_raw}

        for m in mock_attempts:
            user = mock_users_map.get(m.user_id)
            test = mock_tests_map.get(m.mock_test_id)
            completed_str = "Unknown"
            if m.attempt_date:
                from datetime import timezone
                utc_dt = m.attempt_date.replace(tzinfo=timezone.utc)
                local_dt = utc_dt.astimezone()
                completed_str = local_dt.strftime("%Y-%m-%d %H:%M")
            formatted.append({
                "id": m.id,
                "attempt_id": m.id,
                "username": user.username if user else "Unknown",
                "exam_title": test.title if test else "Unknown",
                "score": m.score or 0,
                "total_questions": m.total_questions or 0,
                "percentage": m.percentage or 0.0,
                "status": "PASS" if (m.percentage or 0.0) >= 50.0 else "FAIL",
                "violations_count": mock_violations_map.get(m.id, 0),
                "completed_at": completed_str,
                "exam_type": "MOCK_TEST"
            })

    return formatted

@app.get("/api/attempts/{attempt_id}/result")
def get_attempt_result(attempt_id: int, db: Session = Depends(get_db)):
    res = db.query(ResultModel).filter(ResultModel.attempt_id == attempt_id).first()
    if res:
        user = db.query(UserModel).filter(UserModel.id == res.user_id).first()
        exam = db.query(ExamModel).filter(ExamModel.id == res.exam_id).first()
        violations = db.query(ProctorLogModel).filter(ProctorLogModel.attempt_id == attempt_id).all()
        
        return {
            "id": res.id,
            "username": user.username if user else "Unknown",
            "exam_title": exam.title if exam else "Unknown",
            "exam_type": exam.exam_type if exam else "CERTIFICATION",
            "score": res.score,
            "total_questions": res.total_questions,
            "percentage": res.percentage,
            "status": res.status,
            "violations": [
                {
                    "type": v.violation_type,
                    "description": v.description,
                    "timestamp": v.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                } for v in violations
            ]
        }
        
    mock_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
    if mock_attempt:
        user = db.query(UserModel).filter(UserModel.id == mock_attempt.user_id).first()
        test = db.query(MockTestModel).filter(MockTestModel.id == mock_attempt.mock_test_id).first()
        violations = db.query(ProctorLogModel).filter(ProctorLogModel.attempt_id == attempt_id, ProctorLogModel.is_mock == True).all()
        return {
            "id": mock_attempt.id,
            "username": user.username if user else "Unknown",
            "exam_title": test.title if test else "Unknown",
            "exam_type": "MOCK_TEST",
            "score": mock_attempt.score or 0,
            "total_questions": mock_attempt.total_questions or 0,
            "percentage": mock_attempt.percentage or 0.0,
            "status": "PASS" if (mock_attempt.percentage or 0.0) >= 50.0 else "FAIL",
            "violations": [
                {
                    "type": v.violation_type,
                    "description": v.description,
                    "timestamp": v.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                } for v in violations
            ]
        }
        
    raise HTTPException(status_code=404, detail="Result not found")
    
    return {
        "id": res.id,
        "username": user.username if user else "Unknown",
        "exam_title": exam.title if exam else "Unknown",
        "exam_type": exam.exam_type if exam else "CERTIFICATION",
        "score": res.score,
        "total_questions": res.total_questions,
        "percentage": res.percentage,
        "status": res.status,
        "violations": [
            {
                "type": v.violation_type,
                "description": v.description,
                "timestamp": v.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            } for v in violations
        ]
    }

@app.get("/api/violations")
def get_all_violations(db: Session = Depends(get_db)):
    logs = db.query(ProctorLogModel).order_by(ProctorLogModel.timestamp.desc()).all()
    if not logs:
        return []

    cert_attempt_ids = {l.attempt_id for l in logs if not l.is_mock}
    mock_attempt_ids = {l.attempt_id for l in logs if l.is_mock}

    cert_attempts = db.query(ExamAttemptModel).filter(ExamAttemptModel.id.in_(cert_attempt_ids)).all()
    mock_attempts = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id.in_(mock_attempt_ids)).all()

    cert_attempts_map = {a.id: a for a in cert_attempts}
    mock_attempts_map = {a.id: a for a in mock_attempts}

    cert_user_ids = {a.user_id for a in cert_attempts}
    mock_user_ids = {a.user_id for a in mock_attempts}
    all_user_ids = cert_user_ids.union(mock_user_ids)
    users_map = {
        u.id: u for u in db.query(UserModel).filter(UserModel.id.in_(all_user_ids)).all()
    }

    cert_exam_ids = {a.exam_id for a in cert_attempts}
    exams_map = {
        e.id: e for e in db.query(ExamModel).filter(ExamModel.id.in_(cert_exam_ids)).all()
    }

    mock_test_ids = {a.mock_test_id for a in mock_attempts}
    mock_tests_map = {
        t.id: t for t in db.query(MockTestModel).filter(MockTestModel.id.in_(mock_test_ids)).all()
    }

    formatted = []
    for l in logs:
        if l.is_mock:
            attempt = mock_attempts_map.get(l.attempt_id)
            user = users_map.get(attempt.user_id) if attempt else None
            test = mock_tests_map.get(attempt.mock_test_id) if attempt else None
            formatted.append({
                "id": l.id,
                "attempt_id": l.attempt_id,
                "username": user.username if user else "Unknown",
                "exam_title": test.title if test else "Unknown",
                "violation_type": l.violation_type,
                "description": l.description,
                "timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            })
        else:
            attempt = cert_attempts_map.get(l.attempt_id)
            user = users_map.get(attempt.user_id) if attempt else None
            exam = exams_map.get(attempt.exam_id) if attempt else None
            formatted.append({
                "id": l.id,
                "attempt_id": l.attempt_id,
                "username": user.username if user else "Unknown",
                "exam_title": exam.title if exam else "Unknown",
                "violation_type": l.violation_type,
                "description": l.description,
                "timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            })
    return formatted

# Mock Test: return questions with correct answers + explanations for instant review
@app.get("/api/attempts/{attempt_id}/review")
def get_attempt_review(attempt_id: int, db: Session = Depends(get_db)):
    """
    Returns question-by-question review for MOCK_TEST attempts.
    Includes: the candidate's selected answer, correct answer, and explanation.
    CERTIFICATION exams return 403 — this endpoint is for MOCK_TEST only.
    """
    # Check if ExamAttemptModel
    attempt = db.query(ExamAttemptModel).filter(ExamAttemptModel.id == attempt_id).first()
    if attempt:
        exam_obj = db.query(ExamModel).filter(ExamModel.id == attempt.exam_id).first()
        if not exam_obj:
            raise HTTPException(status_code=404, detail="Exam not found")
        if exam_obj.exam_type != "MOCK_TEST":
            raise HTTPException(status_code=403, detail="Review is only available for Mock Test attempts.")

        questions = db.query(QuestionModel).filter(QuestionModel.exam_id == attempt.exam_id).all()
        answers = db.query(AnswerModel).filter(AnswerModel.attempt_id == attempt_id).all()
        ans_map = {a.question_id: a.selected_option for a in answers}

        review = []
        for q in questions:
            selected = ans_map.get(q.id)
            review.append({
                "question_id": q.id,
                "question_text": q.question_text,
                "option_a": q.option_a,
                "option_b": q.option_b,
                "option_c": q.option_c,
                "option_d": q.option_d,
                "selected_option": selected,
                "correct_option": q.correct_option,
                "is_correct": selected == q.correct_option if selected else False,
                "explanation": q.explanation or ""
            })
        return review
        
    # Check if UserTestAttemptModel
    mock_attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
    if mock_attempt:
        test = db.query(MockTestModel).filter(MockTestModel.id == mock_attempt.mock_test_id).first()
        if not test:
            raise HTTPException(status_code=404, detail="Mock test not found")
            
        test_questions = db.query(MockTestQuestionModel).filter(
            MockTestQuestionModel.mock_test_id == mock_attempt.mock_test_id
        ).order_by(MockTestQuestionModel.question_order).all()
        
        q_ids = [tq.question_id for tq in test_questions]
        questions = db.query(QuestionBankModel).filter(QuestionBankModel.id.in_(q_ids)).all()
        q_dict = {q.id: q for q in questions}
        ordered_questions = [q_dict[tq.question_id] for tq in test_questions if tq.question_id in q_dict]
        
        import json
        ans_map = {}
        if mock_attempt.answers:
            try:
                ans_map = json.loads(mock_attempt.answers)
            except Exception:
                pass
                
        review = []
        for q in ordered_questions:
            selected = ans_map.get(str(q.id)) or ans_map.get(q.id)
            review.append({
                "question_id": q.id,
                "question_text": q.question_text,
                "option_a": q.option_a,
                "option_b": q.option_b,
                "option_c": q.option_c,
                "option_d": q.option_d,
                "selected_option": selected,
                "correct_option": q.correct_option,
                "is_correct": selected == q.correct_option if selected else False,
                "explanation": q.explanation or ""
            })
        return review
        
    raise HTTPException(status_code=404, detail="Attempt not found")

# AWS S3 Upload API
from fastapi import UploadFile, File, Form

# FIX Bug #5: Endpoint path matches Flask proxy exactly (path param, not query param).
@app.post("/api/attempts/{attempt_id}/upload-recording")
def upload_recording(
    attempt_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    print(f"[Scorecard API] Recording upload started for Attempt {attempt_id}...")
    attempt = db.query(ExamAttemptModel).filter(ExamAttemptModel.id == attempt_id).first()
    if not attempt:
        attempt = db.query(UserTestAttemptModel).filter(UserTestAttemptModel.id == attempt_id).first()
        if not attempt:
            raise HTTPException(status_code=404, detail="Attempt not found")

    candidate_id = attempt.user_id

    bucket_name = os.getenv("S3_BUCKET_NAME", "chakorahub-exam-recordings")
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_REGION", "eu-north-1")

    filename = f"candidate_{candidate_id}_attempt_{attempt_id}.webm"
    s3_key = f"exam-recordings/{candidate_id}/{filename}"
    s3_url = f"https://{bucket_name}.s3.{aws_region}.amazonaws.com/{s3_key}"

    # ROOT CAUSE FIX 3a: Log credential presence at the moment of upload so it is
    # visible in FastAPI console exactly why S3 is skipped when credentials are absent.
    print(f"[Scorecard API] AWS_ACCESS_KEY_ID present: {bool(aws_access_key)}")
    print(f"[Scorecard API] AWS_SECRET_ACCESS_KEY present: {bool(aws_secret_key)}")
    print(f"[Scorecard API] AWS_REGION: {aws_region}")
    print(f"[Scorecard API] S3_BUCKET_NAME: {bucket_name}")

    if not aws_access_key or not aws_secret_key:
        print("[Scorecard API] WARNING: AWS credentials missing — saving locally. "
              "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in your .env file.")
        return _save_recording_locally(file, filename, attempt, db)

    # ROOT CAUSE FIX 3b: Read file bytes into memory buffer before passing to boto3.
    # boto3's upload_fileobj() requires a seekable stream. FastAPI's UploadFile.file is
    # a SpooledTemporaryFile — after Flask proxies the multipart upload through requests,
    # the stream position may be non-zero or the underlying SpooledTemporaryFile may have
    # already been read once during FastAPI's own validation. An explicit seek(0) before
    # reading and passing a fresh BytesIO to upload_fileobj eliminates this class of error.
    import io
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, EndpointResolutionError

    try:
        file_bytes = file.file.read()
        if not file_bytes:
            print("[Scorecard API] ERROR: Received empty file bytes — blob was 0 bytes from client.")
            return _save_recording_locally(file, filename, attempt, db, already_read=True, data=file_bytes)

        print(f"[Scorecard API] File received: {len(file_bytes)} bytes. Uploading to S3...")

        s3_client = boto3.client(
            's3',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=aws_region
        )

        # ROOT CAUSE FIX 3c: Use upload_fileobj with a BytesIO — avoids SpooledTemporaryFile
        # stream position issues. No ACL parameter — bucket uses Object Ownership=BucketOwnerEnforced
        # which rejects any ACL parameter and raises AccessControlListNotSupported.
        s3_client.upload_fileobj(
            io.BytesIO(file_bytes),
            bucket_name,
            s3_key,
            ExtraArgs={'ContentType': 'video/webm'}
        )
        print(f"[Scorecard API] S3 upload successful. URL: {s3_url}")
        attempt.recording_url = s3_url
        db.commit()
        return {"recording_url": s3_url, "status": "uploaded_s3"}

    except NoCredentialsError as e:
        # Credentials present as strings but boto3 cannot resolve them (malformed, expired)
        print(f"[Scorecard API] S3 FAILED — NoCredentialsError: {str(e)}")
        return _save_recording_locally(None, filename, attempt, db, already_read=True, data=file_bytes)
    except ClientError as e:
        # IAM permission denied, bucket not found, wrong region, ACL rejected, etc.
        error_code = e.response['Error']['Code']
        error_msg = e.response['Error']['Message']
        print(f"[Scorecard API] S3 FAILED — ClientError [{error_code}]: {error_msg}")
        print(f"[Scorecard API] Full error: {str(e)}")
        return _save_recording_locally(None, filename, attempt, db, already_read=True, data=file_bytes)
    except Exception as e:
        print(f"[Scorecard API] S3 FAILED — Unexpected error [{type(e).__name__}]: {str(e)}")
        return _save_recording_locally(None, filename, attempt, db, already_read=True, data=file_bytes)


def _save_recording_locally(file, filename: str, attempt, db, already_read: bool = False, data: bytes = None):
    """
    Local fallback storage. Accepts either a raw UploadFile or pre-read bytes.
    Separated into a helper so the error code path is clean and explicit.
    """
    try:
        os.makedirs("static/recordings", exist_ok=True)
        local_path = f"static/recordings/{filename}"
        if already_read:
            content = data or b""
        else:
            content = file.file.read() if file else b""

        with open(local_path, "wb") as f:
            f.write(content)

        local_url = f"/static/recordings/{filename}"
        attempt.recording_url = local_url
        db.commit()
        print(f"[Scorecard API] Recording saved locally: {local_url} ({len(content)} bytes)")
        return {"recording_url": local_url, "status": "saved_locally_fallback"}
    except Exception as local_err:
        print(f"[Scorecard API] Local save also failed: {str(local_err)}")
        return {"recording_url": "", "error": str(local_err)}

# ========== NEW ENDPOINTS FOR QUESTION BANK ==========

def parse_uploaded_file(file_content: bytes, filename: str) -> List[Dict[str, Any]]:
    import io
    import re
    import pandas as pd
    
    parsed_rows = []
    
    def clean_option_text(text, prefix_char):
        if text is None:
            return ''
        text_str = str(text).strip()
        pattern = rf'^{prefix_char}[\s\)\.]+\s*'
        return re.sub(pattern, '', text_str, flags=re.IGNORECASE)

    if filename.endswith('.csv'):
        try:
            content_str = file_content.decode('utf-8')
        except UnicodeDecodeError:
            content_str = file_content.decode('latin-1')
            
        df = pd.read_csv(io.StringIO(content_str))
        
        header_row_idx = None
        headers = [str(c).strip() for c in df.columns]
        
        q_idx = next((i for i, v in enumerate(headers) if 'question' in v.lower()), None)
        a_idx = next((i for i, v in enumerate(headers) if 'option a' in v.lower() or 'option_a' in v.lower()), None)
        
        if q_idx is not None and a_idx is not None:
            header_row_idx = -1
        else:
            for idx, row in df.iterrows():
                row_vals = [str(v).strip() for v in row.values]
                q_idx = next((i for i, v in enumerate(row_vals) if 'question' in v.lower()), None)
                a_idx = next((i for i, v in enumerate(row_vals) if 'option a' in v.lower() or 'option_a' in v.lower()), None)
                if q_idx is not None and a_idx is not None:
                    header_row_idx = idx
                    headers = row_vals
                    break
                    
        if header_row_idx is None:
            raise ValueError("Could not find header row containing 'Question' and 'Option A' in CSV.")
            
        col_indices = {}
        for idx, h in enumerate(headers):
            h_lower = h.lower()
            if 'question' in h_lower:
                col_indices['Question'] = idx
            elif 'option a' in h_lower or 'option_a' in h_lower:
                col_indices['Option A'] = idx
            elif 'option b' in h_lower or 'option_b' in h_lower:
                col_indices['Option B'] = idx
            elif 'option c' in h_lower or 'option_c' in h_lower:
                col_indices['Option C'] = idx
            elif 'option d' in h_lower or 'option_d' in h_lower:
                col_indices['Option D'] = idx
            elif 'correct' in h_lower:
                col_indices['Correct Answer'] = idx
            elif 'explanation' in h_lower or 'trap' in h_lower or 'note' in h_lower:
                col_indices['Explanation'] = idx
                
        for req in ['Question', 'Option A', 'Option B', 'Option C', 'Option D']:
            if req not in col_indices:
                raise ValueError(f"Required column '{req}' not found in CSV headers.")
                
        start_row = header_row_idx + 1 if header_row_idx >= 0 else 0
        for i in range(start_row, len(df)):
            row_vals = list(df.iloc[i].values)
            q_val = row_vals[col_indices['Question']]
            if pd.isna(q_val) or str(q_val).strip() == '':
                continue
                
            opt_a_raw = row_vals[col_indices['Option A']]
            opt_b_raw = row_vals[col_indices['Option B']]
            opt_c_raw = row_vals[col_indices['Option C']]
            opt_d_raw = row_vals[col_indices['Option D']]
            
            correct_val = ''
            if 'Correct Answer' in col_indices:
                correct_val = str(row_vals[col_indices['Correct Answer']]).strip().upper()
                if len(correct_val) > 1:
                    if correct_val == str(opt_a_raw).strip().upper() or correct_val == clean_option_text(opt_a_raw, 'A').upper():
                        correct_val = 'A'
                    elif correct_val == str(opt_b_raw).strip().upper() or correct_val == clean_option_text(opt_b_raw, 'B').upper():
                        correct_val = 'B'
                    elif correct_val == str(opt_c_raw).strip().upper() or correct_val == clean_option_text(opt_c_raw, 'C').upper():
                        correct_val = 'C'
                    elif correct_val == str(opt_d_raw).strip().upper() or correct_val == clean_option_text(opt_d_raw, 'D').upper():
                        correct_val = 'D'
            
            if not correct_val:
                correct_val = 'A'
                
            explanation_val = ''
            if 'Explanation' in col_indices:
                explanation_val = str(row_vals[col_indices['Explanation']] or '').strip()
                if pd.isna(row_vals[col_indices['Explanation']]):
                    explanation_val = ''
                    
            parsed_rows.append({
                'Question': str(q_val).strip(),
                'Option A': clean_option_text(opt_a_raw, 'A'),
                'Option B': clean_option_text(opt_b_raw, 'B'),
                'Option C': clean_option_text(opt_c_raw, 'C'),
                'Option D': clean_option_text(opt_d_raw, 'D'),
                'Correct Answer': correct_val,
                'Explanation': explanation_val
            })
            
    else:
        # Excel file - try to import openpyxl
        try:
            import openpyxl
        except ImportError:
            raise ImportError(
                "openpyxl is required for Excel file parsing. "
                "Please install it: pip install openpyxl"
            )
        
        wb = openpyxl.load_workbook(io.BytesIO(file_content), data_only=True)
        sheet_name = 'Python MCQ Mock Test 2' if 'Python MCQ Mock Test 2' in wb.sheetnames else wb.sheetnames[0]
        sheet = wb[sheet_name]
        
        header_row_idx = None
        headers = []
        for r in range(1, sheet.max_row + 1):
            row_vals = [str(sheet.cell(row=r, column=c).value or '').strip() for c in range(1, sheet.max_column + 1)]
            q_idx = next((i for i, v in enumerate(row_vals) if 'question' in v.lower()), None)
            a_idx = next((i for i, v in enumerate(row_vals) if 'option a' in v.lower() or 'option_a' in v.lower()), None)
            if q_idx is not None and a_idx is not None:
                header_row_idx = r
                headers = row_vals
                break
                
        if not header_row_idx:
            raise ValueError("Could not find header row containing 'Question' and 'Option A' in Excel sheet.")
            
        col_mapping = {}
        for idx, h in enumerate(headers):
            h_lower = h.lower()
            if 'question' in h_lower:
                col_mapping['Question'] = idx + 1
            elif 'option a' in h_lower or 'option_a' in h_lower:
                col_mapping['Option A'] = idx + 1
            elif 'option b' in h_lower or 'option_b' in h_lower:
                col_mapping['Option B'] = idx + 1
            elif 'option c' in h_lower or 'option_c' in h_lower:
                col_mapping['Option C'] = idx + 1
            elif 'option d' in h_lower or 'option_d' in h_lower:
                col_mapping['Option D'] = idx + 1
            elif 'correct' in h_lower:
                col_mapping['Correct Answer'] = idx + 1
            elif 'explanation' in h_lower or 'trap' in h_lower or 'note' in h_lower:
                col_mapping['Explanation'] = idx + 1

        required_keys = ['Question', 'Option A', 'Option B', 'Option C', 'Option D']
        for key in required_keys:
            if key not in col_mapping:
                raise ValueError(f"Required column '{key}' not found in Excel sheet headers.")

        for r in range(header_row_idx + 1, sheet.max_row + 1):
            q_val = sheet.cell(row=r, column=col_mapping['Question']).value
            if q_val is None or str(q_val).strip() == '':
                continue
                
            opt_a_raw = sheet.cell(row=r, column=col_mapping['Option A']).value
            opt_b_raw = sheet.cell(row=r, column=col_mapping['Option B']).value
            opt_c_raw = sheet.cell(row=r, column=col_mapping['Option C']).value
            opt_d_raw = sheet.cell(row=r, column=col_mapping['Option D']).value
            
            row_data = {
                'Question': str(q_val).strip(),
                'Option A': clean_option_text(opt_a_raw, 'A'),
                'Option B': clean_option_text(opt_b_raw, 'B'),
                'Option C': clean_option_text(opt_c_raw, 'C'),
                'Option D': clean_option_text(opt_d_raw, 'D')
            }
            
            correct_ans = None
            if 'Correct Answer' in col_mapping:
                correct_ans_val = sheet.cell(row=r, column=col_mapping['Correct Answer']).value
                if correct_ans_val:
                    correct_ans = str(correct_ans_val).strip().upper()
                    if len(correct_ans) > 1:
                        if correct_ans == str(opt_a_raw).strip().upper() or correct_ans == row_data['Option A'].upper():
                            correct_ans = 'A'
                        elif correct_ans == str(opt_b_raw).strip().upper() or correct_ans == row_data['Option B'].upper():
                            correct_ans = 'B'
                        elif correct_ans == str(opt_c_raw).strip().upper() or correct_ans == row_data['Option C'].upper():
                            correct_ans = 'C'
                        elif correct_ans == str(opt_d_raw).strip().upper() or correct_ans == row_data['Option D'].upper():
                            correct_ans = 'D'
                            
            if not correct_ans:
                option_cols = {
                    'A': col_mapping['Option A'],
                    'B': col_mapping['Option B'],
                    'C': col_mapping['Option C'],
                    'D': col_mapping['Option D']
                }
                for opt_char, col_idx in option_cols.items():
                    cell = sheet.cell(row=r, column=col_idx)
                    fill = cell.fill
                    if fill and fill.fill_type == 'solid' and fill.fgColor:
                        color_rgb = str(fill.fgColor.rgb or '').upper()
                        if 'E2EF' in color_rgb or 'C6EF' in color_rgb or color_rgb in ['00E2EFDA', 'E2EFDA', '00C6EFCE', 'C6EFCE']:
                            correct_ans = opt_char
                            break
                
                if not correct_ans:
                    correct_ans = 'A'
                    
            row_data['Correct Answer'] = correct_ans
            row_data['Explanation'] = str(sheet.cell(row=r, column=col_mapping['Explanation']).value or '').strip() if 'Explanation' in col_mapping else ''
            parsed_rows.append(row_data)
            
    return parsed_rows

@app.post("/api/admin/upload-questions/preview")
async def preview_questions(file: UploadFile = File(...)):
    """Parse uploaded CSV/Excel and return preview data without saving"""
    content = await file.read()
    try:
        parsed_rows = parse_uploaded_file(content, file.filename)
        columns = ['Question', 'Option A', 'Option B', 'Option C', 'Option D', 'Correct Answer', 'Explanation']
        return {
            "total_rows": len(parsed_rows),
            "columns": columns,
            "preview": parsed_rows[:10],
            "filename": file.filename        }
    except ImportError as e:
        raise HTTPException(
            status_code=400, 
            detail=f"Missing required library: {str(e)}. Please install: pip install openpyxl"
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"File parsing error: {str(e)}")

# ================================================================
# SAVE QUESTIONS WITH 50,000 LIMIT
# ================================================================
@app.post("/api/admin/upload-questions/save")
async def save_questions_from_file(
    file: UploadFile = File(...),
    category: str = Form("Uncategorized"),
    db: Session = Depends(get_db)
):
    """Save uploaded questions to question_bank"""
    
    # Log what category is received
    print(f"[FastAPI] Received upload with category: '{category}'")
    print(f"[FastAPI] File: {file.filename}")
    
    content = await file.read()
    try:
        parsed_rows = parse_uploaded_file(content, file.filename)
        
        # Ensure category is not empty
        if not category or category.strip() == '':
            category = 'Uncategorized'
            print(f"[FastAPI] Category was empty, defaulting to: '{category}'")
        
        # Check file size limit
        if len(parsed_rows) > MAX_UPLOAD_QUESTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File contains {len(parsed_rows)} questions. Maximum allowed is {MAX_UPLOAD_QUESTIONS:,}. Please split your file into smaller chunks."
            )
        
        saved_count = 0
        errors = []
        
        for idx, row in enumerate(parsed_rows):
            try:
                if not row['Question']:
                    errors.append(f"Row {idx+2}: Missing question text")
                    continue
                
                question = QuestionBankModel(
                    category=category,
                    question_text=row['Question'],
                    option_a=row['Option A'],
                    option_b=row['Option B'],
                    option_c=row['Option C'],
                    option_d=row['Option D'],
                    correct_option=row['Correct Answer'],
                    explanation=row['Explanation'] if row['Explanation'] else None
                )
                db.add(question)
                saved_count += 1
                    
            except Exception as e:
                errors.append(f"Row {idx+2}: {str(e)}")
        
        if saved_count == 0:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "No questions were saved. Check row errors and data formats.",
                    "total_rows": len(parsed_rows),
                    "saved_count": saved_count,
                    "errors": errors[:50]
                }
            )

        db.commit()
        
        print(f"[FastAPI] Successfully saved {saved_count} questions with category: '{category}'")
        
        return {
            "success": True,
            "saved_count": saved_count,
            "total_rows": len(parsed_rows),
            "errors": errors
        }
    except ImportError as e:
        raise HTTPException(
            status_code=400, 
            detail=f"Missing required library: {str(e)}. Please install: pip install openpyxl"
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"[FastAPI] Save error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Save error: {str(e)}")

@app.get("/api/admin/question-bank")
def get_question_bank(
    page: Optional[int] = None,
    page_size: Optional[int] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Get questions from bank with filtering and pagination"""
    query = db.query(QuestionBankModel)
    
    if category and category != "All":
        query = query.filter(QuestionBankModel.category == category)
    
    if search:
        query = query.filter(QuestionBankModel.question_text.contains(search))
    
    total = query.count()
    
    effective_page = page if page is not None else 1
    
    if page_size is not None:
        effective_page_size = page_size
    elif limit is not None:
        effective_page_size = limit
    else:
        effective_page_size = 100
        
    offset = (effective_page - 1) * effective_page_size
    questions = query.order_by(QuestionBankModel.created_at.desc()).offset(offset).limit(effective_page_size).all()
    
    return {
        "total": total,
        "page": effective_page,
        "page_size": effective_page_size,
        "questions": questions
    }

@app.get("/api/admin/categories")
def get_categories(db: Session = Depends(get_db)):
    """Get unique categories from question bank with counts"""
    results = db.query(
        QuestionBankModel.category,
        func.count(QuestionBankModel.id)
    ).group_by(QuestionBankModel.category).all()
    
    categories = []
    for r in results:
        cat_name = r[0] if r[0] else "Uncategorized"
        categories.append({"category": cat_name, "count": r[1]})
    return categories

# ================================================================
# DELETE CATEGORY
# ================================================================
@app.delete("/api/admin/categories/{category_name}")
def delete_category(category_name: str, db: Session = Depends(get_db)):
    """Delete a category and all its questions"""
    # Check if category exists
    category_exists = db.query(QuestionBankModel).filter(
        QuestionBankModel.category == category_name
    ).first()
    
    if not category_exists:
        raise HTTPException(status_code=404, detail=f"Category '{category_name}' not found")
    
    # Count questions in this category
    question_count = db.query(QuestionBankModel).filter(
        QuestionBankModel.category == category_name
    ).count()
    
    # Delete all questions in this category
    deleted = db.query(QuestionBankModel).filter(
        QuestionBankModel.category == category_name
    ).delete(synchronize_session=False)
    
    db.commit()
    
    return {
        "success": True,
        "message": f"Category '{category_name}' deleted successfully",
        "deleted_count": deleted
    }

@app.delete("/api/admin/question-bank/{question_id}")
def delete_question(question_id: int, db: Session = Depends(get_db)):
    """Delete a question from the bank"""
    question = db.query(QuestionBankModel).filter(QuestionBankModel.id == question_id).first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    
    db.delete(question)
    db.commit()
    return {"message": "Question deleted successfully"}

# ================================================================
# NEW ENDPOINTS FOR MOCK TESTS with auto-publish
# ================================================================

@app.post("/api/admin/mock-tests", response_model=MockTestResponse)
def create_mock_test(payload: MockTestCreate, db: Session = Depends(get_db)):
    """Create a new mock test from selected questions"""
    
    # Validate questions exist
    questions = db.query(QuestionBankModel).filter(QuestionBankModel.id.in_(payload.question_ids)).all()
    if len(questions) != len(payload.question_ids):
        raise HTTPException(status_code=400, detail="Some questions not found")
    
    # Create mock test with is_published=1 (auto-publish)
    mock_test = MockTestModel(
        title=payload.title,
        category=payload.category,
        description=payload.description,
        duration_minutes=payload.duration_minutes,
        scheduled_date=payload.scheduled_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        is_published=1  # Auto-publish so it appears immediately
    )
    db.add(mock_test)
    db.commit()
    db.refresh(mock_test)
    
    # Add questions to test with order
    for order, q_id in enumerate(payload.question_ids):
        test_question = MockTestQuestionModel(
            mock_test_id=mock_test.id,
            question_id=q_id,
            question_order=order
        )
        db.add(test_question)
    
    db.commit()
    
    return MockTestResponse(
        id=mock_test.id,
        title=mock_test.title,
        category=mock_test.category,
        description=mock_test.description,
        duration_minutes=mock_test.duration_minutes,
        scheduled_date=mock_test.scheduled_date,
        start_time=mock_test.start_time,
        end_time=mock_test.end_time,
        is_published=mock_test.is_published,
        total_questions=len(payload.question_ids)
    )

@app.get("/api/admin/mock-tests", response_model=List[MockTestResponse])
def get_mock_tests(db: Session = Depends(get_db)):
    """Get all mock tests with question numbers for display"""
    tests = db.query(MockTestModel).order_by(MockTestModel.created_at.desc()).all()
    
    result = []
    for test in tests:
        # Get questions with their order
        test_questions = db.query(MockTestQuestionModel).filter(
            MockTestQuestionModel.mock_test_id == test.id
        ).order_by(MockTestQuestionModel.question_order).all()
        
        q_count = len(test_questions)
        
        # Build question numbers list for display in create mock test
        questions_with_numbers = []
        for idx, tq in enumerate(test_questions, start=1):
            q = db.query(QuestionBankModel).filter(QuestionBankModel.id == tq.question_id).first()
            if q:
                questions_with_numbers.append({
                    "number": idx,
                    "question_id": q.id,
                    "question_text": q.question_text[:100] + "..." if len(q.question_text) > 100 else q.question_text,
                    "category": q.category
                })
        
        result.append(MockTestResponse(
            id=test.id,
            title=test.title,
            category=test.category,
            description=test.description,
            duration_minutes=test.duration_minutes,
            scheduled_date=test.scheduled_date,
            start_time=test.start_time,
            end_time=test.end_time,
            is_published=test.is_published,
            total_questions=q_count,
            questions_with_numbers=questions_with_numbers
        ))
    
    return result

@app.put("/api/admin/mock-tests/{test_id}/publish")
def publish_mock_test(test_id: int, db: Session = Depends(get_db)):
    """Toggle publish status of mock test"""
    test = db.query(MockTestModel).filter(MockTestModel.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")
    
    test.is_published = 1 if test.is_published == 0 else 0
    db.commit()
    
    return {"is_published": test.is_published}

@app.delete("/api/admin/mock-tests/{test_id}")
def delete_mock_test(test_id: int, db: Session = Depends(get_db)):
    """Delete a mock test"""
    test = db.query(MockTestModel).filter(MockTestModel.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")
    
    # Delete related questions first
    db.query(MockTestQuestionModel).filter(MockTestQuestionModel.mock_test_id == test_id).delete()
    # Delete related attempts to prevent foreign key constraint violation
    db.query(UserTestAttemptModel).filter(UserTestAttemptModel.mock_test_id == test_id).delete()
    db.delete(test)
    db.commit()
    
    return {"message": "Test deleted successfully"}

# ================================================================
# CANDIDATE UPCOMING TESTS with time checks
# ================================================================

@app.get("/api/candidate/upcoming-tests")
def get_upcoming_tests(user_id: int, db: Session = Depends(get_db)):
    """Get upcoming published mock tests for candidate."""
    from datetime import datetime, timedelta
    
    print(f"[FastAPI] Fetching upcoming tests for user {user_id}")
    
    app_tz = ZoneInfo(APP_TIMEZONE)
    now = datetime.now(app_tz)
    today_str = now.strftime("%Y-%m-%d")
    
    # Query published tests scheduled for today
    tests = db.query(MockTestModel).filter(
        MockTestModel.is_published == 1,
        MockTestModel.scheduled_date == today_str
    ).order_by(
        MockTestModel.start_time.asc()
    ).all()
    
    print(f"[FastAPI] Found {len(tests)} published tests for today")
    
    upcoming = []
    for test in tests:
        # Build start datetime and keep existing 24-hour window behavior.
        scheduled_start = datetime.strptime(
            f"{test.scheduled_date} {test.start_time}",
            "%Y-%m-%d %H:%M"
        ).replace(tzinfo=app_tz)
        test_end_time = scheduled_start + timedelta(hours=24)
        
        # Skip if test window has expired
        if now > test_end_time:
            continue
        
        # Compare using datetimes to avoid string-ordering issues.
        is_upcoming = now < scheduled_start
        is_active = scheduled_start <= now <= test_end_time
        
        # Count ALL attempts
        total_attempts = db.query(UserTestAttemptModel).filter(
            UserTestAttemptModel.user_id == user_id,
            UserTestAttemptModel.mock_test_id == test.id
        ).count()
        
        # Count completed attempts
        completed_attempts = db.query(UserTestAttemptModel).filter(
            UserTestAttemptModel.user_id == user_id,
            UserTestAttemptModel.mock_test_id == test.id,
            UserTestAttemptModel.status == "completed"
        ).count()
        
        # Check for in-progress attempt
        in_progress = db.query(UserTestAttemptModel).filter(
            UserTestAttemptModel.user_id == user_id,
            UserTestAttemptModel.mock_test_id == test.id,
            UserTestAttemptModel.status.in_(["in_progress", "started"])
        ).first()

        # Hide only when all attempts are exhausted and no resumable attempt exists.
        if total_attempts >= MAX_ATTEMPTS_PER_TEST and not in_progress:
            continue
        
        remaining_attempts = max(0, MAX_ATTEMPTS_PER_TEST - total_attempts)
        
        upcoming.append({
            "id": test.id,
            "title": test.title,
            "category": test.category,
            "description": test.description,
            "duration_minutes": test.duration_minutes,
            "scheduled_date": test.scheduled_date,
            "start_time": test.start_time,
            "end_time": test.end_time,
            "attempts_used": completed_attempts,
            "total_attempts": total_attempts,
            "remaining_attempts": remaining_attempts,
            "has_in_progress": bool(in_progress),
            "can_attempt": bool(in_progress) or (remaining_attempts > 0 and is_active),
            "is_upcoming": is_upcoming,
            "is_active": is_active,
            "starts_at": f"{test.scheduled_date} {test.start_time}",
            "expires_at": test_end_time.strftime("%Y-%m-%d %H:%M")
        })
    
    print(f"[FastAPI] Returning {len(upcoming)} upcoming tests")
    return upcoming


@app.get("/api/candidate/previous-attempts")
def get_previous_attempts(user_id: int, db: Session = Depends(get_db)):
    """Get candidate's previous mock test attempts"""
    print(f"[FastAPI] Fetching previous attempts for user {user_id}")
    
    attempts = db.query(UserTestAttemptModel).filter(
        UserTestAttemptModel.user_id == user_id,
        UserTestAttemptModel.status == "completed"
    ).order_by(UserTestAttemptModel.attempt_date.desc()).all()
    
    print(f"[FastAPI] Found {len(attempts)} completed attempts")
    
    result = []
    for attempt in attempts:
        test = db.query(MockTestModel).filter(MockTestModel.id == attempt.mock_test_id).first()
        if test:
            # Get attempt number
            previous = db.query(UserTestAttemptModel).filter(
                UserTestAttemptModel.user_id == user_id,
                UserTestAttemptModel.mock_test_id == attempt.mock_test_id,
                UserTestAttemptModel.id < attempt.id,
                UserTestAttemptModel.status == "completed"
            ).count()
            attempt_number = previous + 1
            
            attempt_date_str = "Unknown"
            if attempt.attempt_date:
                from datetime import timezone
                utc_dt = attempt.attempt_date.replace(tzinfo=timezone.utc)
                local_dt = utc_dt.astimezone()
                attempt_date_str = local_dt.strftime("%Y-%m-%d %H:%M")
            result.append({
                "id": attempt.id,
                "test_title": test.title,
                "score": attempt.score or 0,
                "total_questions": attempt.total_questions or 0,
                "percentage": attempt.percentage or 0.0,
                "attempt_date": attempt_date_str,
                "attempt_number": attempt_number
            })
    
    print(f"[FastAPI] Returning {len(result)} previous attempts")
    return result

def get_attempt_number(attempt, db):
    """Get the attempt number (1st, 2nd, 3rd) for a given attempt"""
    previous = db.query(UserTestAttemptModel).filter(
        UserTestAttemptModel.user_id == attempt.user_id,
        UserTestAttemptModel.mock_test_id == attempt.mock_test_id,
        UserTestAttemptModel.id < attempt.id,
        UserTestAttemptModel.status == "completed"
    ).count()
    return previous + 1

# New endpoint to convert question_bank to exam format when taking mock test
@app.get("/api/mock-tests/{test_id}/questions", response_model=List[QuestionResponse])
def get_mock_test_questions(test_id: int, db: Session = Depends(get_db)):
    """Get questions for a mock test (converted to QuestionModel format)"""
    # Get the mock test
    mock_test = db.query(MockTestModel).filter(MockTestModel.id == test_id).first()
    if not mock_test:
        raise HTTPException(status_code=404, detail="Mock test not found")
    
    # Get linked questions from question_bank
    test_questions = db.query(MockTestQuestionModel).filter(
        MockTestQuestionModel.mock_test_id == test_id
    ).order_by(MockTestQuestionModel.question_order).all()
    
    # Convert to QuestionModel-like response (for exam_handler.js compatibility)
    questions = []
    for tq in test_questions:
        q = db.query(QuestionBankModel).filter(QuestionBankModel.id == tq.question_id).first()
        if q:
            questions.append({
                "id": q.id,
                "exam_id": test_id,  # Use test_id as exam_id
                "question_text": q.question_text,
                "option_a": q.option_a,
                "option_b": q.option_b,
                "option_c": q.option_c,
                "option_d": q.option_d,
                "correct_option": q.correct_option,
                "explanation": q.explanation or ""
            })
    
    return questions

# Run block for local testing
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ope_service:app", host="0.0.0.0", port=8500, reload=True)