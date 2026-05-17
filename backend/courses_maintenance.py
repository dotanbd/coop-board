import argparse
from datetime import datetime, timedelta

from main import (
    SessionLocal,
    DBAssignment,
    DBCourse,
    DBAttachment,
    DBUserAssignment,
    DBAttachmentLike,
    DBAuditLog,
    user_courses,
    s3_client, DBSummary
)

BUCKET_NAME = "teaspoon-files"


def main():
    parser = argparse.ArgumentParser(description="Teaspoon Course Maintenance Script")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="DANGER: Deep clean inactive courses and all related data."
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        one_year_ago = datetime.utcnow() - timedelta(days=365)
        all_courses = db.query(DBCourse).all()

        # =====================================================================
        # TEMPORARY PHASE 1: SAFEGUARD ACTIVE COURSES (Remove after first run)
        # =====================================================================
        print("🛠️  Phase 1: Safeguarding active courses with relational data...")
        now = datetime.utcnow()
        safeguarded_count = 0

        for course in all_courses:
            # 1. Check if course has ANY assignments (which includes summaries)
            has_assignments = db.query(DBAssignment).filter(DBAssignment.course_code == course.code).first() is not None

            has_summaries = db.query(DBSummary).filter(DBSummary.courseCode == course.code).first() is not None

            # 2. Check if course is in ANY user's myCourses list
            # NOTE: If your user_courses table links by ID instead of code, change .c.course_code to .c.course_id
            has_users = db.execute(
                user_courses.select().where(user_courses.c.course_code == course.code)
            ).fetchone() is not None

            # If the course is currently in use, update its timestamp to NOW
            if has_assignments or has_users or has_summaries:
                if course.last_edited is None or course.last_edited < one_year_ago:
                    course.last_edited = now
                    safeguarded_count += 1

        db.commit()
        print(f"✅ Safely updated 'last_edited' for {safeguarded_count} populated courses.\n")
        # =====================================================================

        active_courses = []
        inactive_courses = []

        print("🔍 Scanning courses for activity...")

        for course in all_courses:
            # Personal assignments safeguard
            if course.code == "9990999":
                continue

            # Using the vitality tracker
            if course.last_edited is not None and course.last_edited >= one_year_ago:
                active_courses.append(course)
            else:
                inactive_courses.append(course)

        print(f"\n📊 Audit Complete:")
        print(f"   - Active Courses (Last 365 days): {len(active_courses)}")
        print(f"   - Inactive Courses (Orphaned): {len(inactive_courses)}")

        if not args.prune:
            print("\n🛡️  DRY RUN MODE (No data was changed)")
            if inactive_courses:
                print("The following courses are flagged for complete deep-clean removal:")
                for c in inactive_courses:
                    print(f"   ❌ {c.code} - {c.name}")
                print("\nTo permanently wipe these and all traces, run the script with the --prune flag.")
            else:
                print("No inactive courses found. Your database is perfectly clean!")

        else:
            print("\n🔥 EXECUTION MODE: Deep cleaning inactive courses...")
            deleted_files = 0
            for c in inactive_courses:
                print(f"\n   Targeting {c.code} - {c.name}...")

                # 1. SWEEP COURSE AUDIT LOGS
                db.query(DBAuditLog).filter(
                    DBAuditLog.entity_type == "COURSE",
                    DBAuditLog.entity_id == c.code
                ).delete(synchronize_session=False)

                # 2. SWEEP FOLLOWED COURSES (user_courses association table)
                db.execute(user_courses.delete().where(user_courses.c.course_code == c.code))

                assignments = db.query(DBAssignment).filter(DBAssignment.courseCode == c.code).all()
                for assignment in assignments:

                    # 3. SWEEP ASSIGNMENT AUDIT LOGS
                    # Using .like() because the ID format is "{id}:{courseCode} - {title}"
                    db.query(DBAuditLog).filter(
                        DBAuditLog.entity_type == "ASSIGNMENT",
                        DBAuditLog.entity_id.like(f"{assignment.id}:%")
                    ).delete(synchronize_session=False)

                    # 4. SWEEP USER GRADES & COMPLETIONS
                    db.query(DBUserAssignment).filter(
                        DBUserAssignment.assignment_id == assignment.id
                    ).delete(synchronize_session=False)

                    attachments = db.query(DBAttachment).filter(DBAttachment.assignment_id == assignment.id).all()
                    for attachment in attachments:

                        # 5. SWEEP ATTACHMENT LIKES
                        db.query(DBAttachmentLike).filter(
                            DBAttachmentLike.attachment_id == attachment.id
                        ).delete(synchronize_session=False)

                        # 6. SWEEP MINIO FILES
                        try:
                            s3_client.delete_object(Bucket=BUCKET_NAME, Key=attachment.object_name)
                            print(f"      ☁️  Deleted file from MinIO: {attachment.object_name}")
                            deleted_files += 1
                        except Exception as e:
                            print(f"      ⚠️  Could not delete MinIO file {attachment.object_name}: {e}")

                        # 7. SWEEP ATTACHMENT ROW
                        db.delete(attachment)

                    # 8. SWEEP ASSIGNMENT ROW
                    db.delete(assignment)

                # 9. SWEEP COURSE ROW
                db.delete(c)
                print(f"   🗑️  Successfully scrubbed course {c.code} and all its dependencies.")

            # Commit the massive transaction!
            db.commit()
            print(f"\n✅ Successfully performed deep clean on {len(inactive_courses)} inactive courses.\n{deleted_files}"
                  f" files were deleted from MinIO.")

    except Exception as e:
        print(f"\n❌ Script failed: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    main()