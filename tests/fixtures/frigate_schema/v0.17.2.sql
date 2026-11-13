-- Frigate v0.17.2 database schema (generated -- do not hand-edit).
-- Source: github.com/blakeblackshear/frigate @ v0.17.2 (3d4dd3ac4b00e7257bd3412608a783001d7d77ed)
--   frigate/models.py + migrations/001_create_events_table.py .. 032_add_password_changed_at.py (32 files)
-- Built by tools/frigate_schema_fixture.py: upstream migrations run with peewee 3.17.9 / peewee_migrate 1.14.3
-- against an empty SQLite file, then sqlite_master dumped (migratehistory omitted).

CREATE TABLE "event" ("id" VARCHAR(30) NOT NULL PRIMARY KEY, "label" VARCHAR(20) NOT NULL, "camera" VARCHAR(20) NOT NULL, "start_time" DATETIME NOT NULL, "end_time" DATETIME, "top_score" REAL, "false_positive" INTEGER, "zones" JSON NOT NULL, "thumbnail" TEXT , "has_clip" INTEGER NOT NULL, "has_snapshot" INTEGER NOT NULL, "region" JSON, "box" JSON, "area" INTEGER, "retain_indefinitely" INTEGER NOT NULL, "sub_label" VARCHAR(100), "ratio" REAL, "plus_id" VARCHAR(30), "score" REAL, "model_hash" VARCHAR(32), "detector_type" VARCHAR(32), "model_type" VARCHAR(32), "data" JSON NOT NULL);

CREATE TABLE "export" ("id" VARCHAR(30) NOT NULL PRIMARY KEY, "camera" VARCHAR(20) NOT NULL, "name" VARCHAR(100) NOT NULL, "date" DATETIME NOT NULL, "video_path" VARCHAR(255) NOT NULL, "thumb_path" VARCHAR(255) NOT NULL, "in_progress" INTEGER NOT NULL);

CREATE TABLE "previews" ("id" VARCHAR(30) NOT NULL PRIMARY KEY, "camera" VARCHAR(20) NOT NULL, "path" VARCHAR(255) NOT NULL, "start_time" DATETIME NOT NULL, "end_time" DATETIME NOT NULL, "duration" REAL NOT NULL);

CREATE TABLE "recordings" ("id" VARCHAR(30) NOT NULL PRIMARY KEY, "camera" VARCHAR(20) NOT NULL, "path" VARCHAR(255) NOT NULL, "start_time" DATETIME NOT NULL, "end_time" DATETIME NOT NULL, "duration" REAL NOT NULL, "objects" INTEGER, "motion" INTEGER, "segment_size" REAL NOT NULL, "dBFS" INTEGER, "regions" INTEGER);

CREATE TABLE "regions" ("camera" VARCHAR(20) NOT NULL PRIMARY KEY, "last_update" DATETIME NOT NULL, "grid" JSON);

CREATE TABLE "reviewsegment" ("id" VARCHAR(30) NOT NULL PRIMARY KEY, "camera" VARCHAR(20) NOT NULL, "start_time" DATETIME NOT NULL, "end_time" DATETIME, "severity" VARCHAR(30) NOT NULL, "thumb_path" VARCHAR(255) NOT NULL, "data" JSON NOT NULL);

CREATE TABLE "timeline" ("timestamp" DATETIME NOT NULL, "camera" VARCHAR(20) NOT NULL, "source" VARCHAR(20) NOT NULL, "source_id" VARCHAR(30), "class_type" VARCHAR(50) NOT NULL, "data" JSON);

CREATE TABLE trigger (
            camera VARCHAR(20) NOT NULL,
            name VARCHAR NOT NULL,
            type VARCHAR(10) NOT NULL,
            model VARCHAR(30) NOT NULL,
            data TEXT NOT NULL,
            threshold REAL,
            embedding BLOB,
            triggering_event_id VARCHAR(30),
            last_triggered DATETIME,
            PRIMARY KEY (camera, name)
        );

CREATE TABLE "user" ("username" VARCHAR(30) NOT NULL PRIMARY KEY, "password_hash" VARCHAR(120) NOT NULL, "notification_tokens" JSON NOT NULL, "role" VARCHAR(20) NOT NULL DEFAULT 'admin', password_changed_at DATETIME NULL);

CREATE TABLE "userreviewstatus" (
            "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            "user_id" VARCHAR(30) NOT NULL,
            "review_segment_id" VARCHAR(30) NOT NULL,
            "has_been_reviewed" INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY ("review_segment_id") REFERENCES "reviewsegment" ("id") ON DELETE CASCADE
        );

CREATE INDEX "event_camera" ON "event" ("camera");

CREATE INDEX "event_label" ON "event" ("label");

CREATE INDEX "event_label_start_time" ON "event" ("label", "start_time" DESC);

CREATE INDEX "event_start_time_end_time" ON "event" ("start_time" DESC, "end_time" DESC);

CREATE INDEX "export_camera" ON "export" ("camera");

CREATE INDEX "recordings_activity" ON "recordings" ("camera", "start_time" DESC, "regions");

CREATE INDEX "recordings_api_recordings_summary" ON "recordings" ("camera", "start_time" DESC, "duration", "motion", "objects");

CREATE INDEX "recordings_camera" ON "recordings" ("camera");

CREATE INDEX "recordings_camera_segment_size" ON "recordings" ("camera", "segment_size");

CREATE INDEX "recordings_camera_start_time_end_time" ON "recordings" ("camera", "start_time" DESC, "end_time" DESC);

CREATE UNIQUE INDEX "recordings_path" ON "recordings" ("path");

CREATE INDEX "recordings_start_time" ON "recordings" ("start_time");

CREATE INDEX "review_segment_camera" ON "reviewsegment" ("camera");

CREATE INDEX "review_segment_start_time_end_time" ON "reviewsegment" ("start_time" DESC, "end_time" DESC);

CREATE INDEX "timeline_camera" ON "timeline" ("camera");

CREATE INDEX "timeline_source" ON "timeline" ("source");

CREATE INDEX "timeline_source_id" ON "timeline" ("source_id");

CREATE UNIQUE INDEX "userreviewstatus_user_segment" ON "userreviewstatus" ("user_id", "review_segment_id");
