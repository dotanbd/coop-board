import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import the DBUser model from your current backend file
from main import DBUser

# Setup database connection
DB_PATH = "./data/teaspoon_v1.db"

if not os.path.exists(DB_PATH):
    print(f"❌ Database not found at {DB_PATH}.")
    print("👉 Make sure you have started your Uvicorn server at least once to build the database!")
    exit(1)

engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = SessionLocal()

# Grab the first user created in the database
user = db.query(DBUser).first()

if user:
    # Promote to an owner
    user.role = "owner"
    db.commit()
    print(f"✅ Success! User '{user.name}' ({user.email}) has been promoted to Owner.")
    print("👉 Refresh your React frontend to see your new admin privileges (like the delete buttons).")
else:
    print("❌ No users found in the database.")
    print("👉 Go to your frontend (http://localhost:5173) and log in via Google first so your account is created!")

db.close()