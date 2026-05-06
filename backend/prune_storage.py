import argparse
from sqlalchemy.orm import Session
from main import SessionLocal, DBAttachment, s3_client, BUCKET_NAME


def prune_large_files(size_limit_mb: float):
    db = SessionLocal()
    size_bytes_limit = size_limit_mb * 1024 * 1024

    print(f"\n🔍 Scanning MinIO for files larger than {size_limit_mb} MB...\n")

    attachments = db.query(DBAttachment).all()
    deleted_count = 0
    freed_bytes = 0

    for att in attachments:
        try:
            # Ask MinIO for the metadata (including file size) of this object
            response = s3_client.head_object(Bucket=BUCKET_NAME, Key=att.object_name)
            file_size_bytes = response.get('ContentLength', 0)

            if file_size_bytes > size_bytes_limit:
                file_size_mb = file_size_bytes / 1024 / 1024
                print(f"🗑️ Deleting {att.filename} ({file_size_mb:.2f} MB)...")

                # 1. Delete the physical file to save hard drive space
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=att.object_name)

                # 2. Delete the database link so the UI doesn't show a broken attachment
                db.delete(att)

                freed_bytes += file_size_bytes
                deleted_count += 1

        except Exception as e:
            # If the file is already missing from MinIO, clean up the ghost database row
            if "404" in str(e):
                print(f"⚠️ Ghost file detected (missing from MinIO). Cleaning up DB link: {att.filename}")
                db.delete(att)
            else:
                print(f"⚠️ Error checking {att.filename}: {e}")

    # Commit all the database link deletions
    db.commit()
    db.close()

    total_freed_mb = freed_bytes / 1024 / 1024
    print(f"\n✅ Pruning Complete!")
    print(f"❌ Deleted {deleted_count} heavy files.")
    print(f"💾 Freed up {total_freed_mb:.2f} MB of permanent storage.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete heavy attachments to save space.")
    parser.add_argument(
        "--size",
        type=float,
        default=15.0,
        help="Minimum size in MB. Files larger than this will be deleted."
    )
    args = parser.parse_args()

    prune_large_files(args.size)