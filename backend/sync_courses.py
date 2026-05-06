import re
import PyPDF2
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import the DBCourse model directly from your backend file
from main import DBCourse

# --- CONFIGURATION ---
CATALOG_PDF_PATH = "catalog.pdf"
SQLALCHEMY_DATABASE_URL = "sqlite:///./data/teaspoon_v1.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def fix_hebrew(text):
    """Reverses Hebrew text visually extracted from PDFs and fixes brackets."""
    text = text.strip()
    # Check if there are any Hebrew characters in the string
    if any('\u0590' <= c <= '\u05FF' for c in text):
        # Reverse the entire string
        text = text[::-1]
        # Since we reversed the string, '(' became ')' and vice versa. Let's fix them.
        text = text.translate(str.maketrans('()[]{}', ')(][}{'))
    return text


def extract_courses_from_pdf(pdf_path):
    courses = {}

    # ⚠️ STRICT REGEX: Exactly 0 + 3 digits + 0 + 3 digits
    course_pattern = re.compile(r'\b(0\d{3}0\d{3})\b')

    # Common syllabus words that indicate this is a description line, not a title line
    bad_keywords = ["קדם", "צמוד", "זיכוי", "מומלץ", "שעות", "הרצאה", "תרגול", "מעבדה", "סילבוס", "פקולט"]

    with open(pdf_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)

        # Pages 73 to 308 (0-indexed, so 72 to 308)
        start_page = 72
        end_page = min(308, len(reader.pages))

        for page_num in range(start_page, end_page):
            text = reader.pages[page_num].extract_text()
            if not text:
                continue

            # Process line by line
            for line in text.split('\n'):
                line = line.strip()

                # Rule 1: Find the strict 8-digit catalog code
                match = course_pattern.search(line)
                if not match:
                    continue

                raw_code = match.group(1)

                # Rule 2: Code MUST be at the very start or end of the line
                if not (line.startswith(raw_code) or line.endswith(raw_code)):
                    continue

                # Rule 3: Skip syllabus description lines
                if any(word in line for word in bad_keywords):
                    continue

                # Remove the 8-digit code from the line so we are left with Name + Spaces + Hours
                remainder = line.replace(raw_code, '').strip()

                # Rule 4: Split by 2+ spaces OR by decimal floats (like 5.0 or 0.5) to slice off the credits columns
                chunks = re.split(r'\s{2,}|\b\d+\.\d+\b', remainder)

                # Filter out chunks that only contain numbers/dashes (the hours columns)
                valid_chunks = [c for c in chunks if re.search(r'[a-zA-Z\u0590-\u05FF]', c)]

                if not valid_chunks:
                    continue

                # The course name is usually the chunk containing the actual letters
                name_str = max(valid_chunks, key=len)

                # Fix the backwards PDF Hebrew
                clean_name = fix_hebrew(name_str)

                # ✨ NEW: Remove all special characters (keep only letters, numbers, spaces, and quotes/geresh for acronyms)
                clean_name = re.sub(r'[^a-zA-Z\u0590-\u05FF0-9\s\"\'״]', ' ', clean_name)

                # ✨ NEW: Strip multiple isolated numbers at the end with spaces between them (e.g., " 3 2")
                # This protects "Physics 1" (single trailing number) but drops "Physics 1 3 2" down to "Physics 1"
                clean_name = re.sub(r'(?<=\s\d)\s+\d+[\s\d]*$', '', clean_name)
                clean_name = re.sub(r'\s+\d+\s+\d+[\s\d]*$', '', clean_name)

                # Clean up any remaining extra spaces or dangling punctuation
                clean_name = re.sub(r'\s+', ' ', clean_name).strip()
                clean_name = re.sub(r'^[\-\|\.\_]+', '', clean_name).strip()
                clean_name = re.sub(r'[\-\|\.\_]+$', '', clean_name).strip()

                # Final Sanity Check: Must be a reasonable length
                if 3 < len(clean_name) < 80:
                    # ✨ THE MAGIC: Convert 0-XXX-0-XXX to standard XXX-0-XXX (7 digits) ✨
                    actual_code = raw_code[1:8]
                    courses[actual_code] = clean_name

    return courses


def sync_courses():
    db = SessionLocal()
    print(f"⏳ Reading catalog from {CATALOG_PDF_PATH} (Pages 73-308)...")

    try:
        courses_data = extract_courses_from_pdf(CATALOG_PDF_PATH)
        print(f"📦 Successfully parsed {len(courses_data)} valid courses.")

        print("🗑️ Wiping existing courses table...")
        db.query(DBCourse).delete()
        db.commit()

        print("✍️ Rebuilding database with 7-digit codes...")
        added_count = 0

        for code, name in courses_data.items():
            new_course = DBCourse(code=code, name=name)
            db.add(new_course)
            added_count += 1

        db.commit()
        print(f"✅ Sync complete! Inserted {added_count} courses into the database.")

        print("\n🔍 PREVIEW OF THE FIRST 10 COURSES EXTRACTED:")
        print("-" * 50)
        for idx, (code, name) in enumerate(list(courses_data.items())[:10]):
            print(f"[{code}] : {name}")
        print("-" * 50)

    except FileNotFoundError:
        print(f"❌ Error: Could not find '{CATALOG_PDF_PATH}'.")
    except Exception as e:
        print(f"❌ Error during sync: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    sync_courses()