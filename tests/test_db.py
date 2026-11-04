from pathlib import Path

from app.db import Database


def test_folder_and_file_lifecycle(tmp_path: Path):
    db = Database(tmp_path / "test.sqlite3")
    folder = db.create_folder("videos", None)
    child = db.create_folder("2024", folder["id"])
    file_item = db.create_file(
        name="demo.mp4",
        parent_id=child["id"],
        size=10,
        mime="video/mp4",
        sha256="abc",
        chat_id="-100123",
        parts=[
            {"part_index": 0, "tg_message_id": 11, "size": 6},
            {"part_index": 1, "tg_message_id": 12, "size": 4},
        ],
    )

    assert [i["name"] for i in db.list_items(None)] == ["videos"]
    assert [i["id"] for i in db.list_items(folder["id"])] == [child["id"]]
    assert [p["tg_message_id"] for p in db.get_parts(file_item["id"])] == [11, 12]
    assert [i["name"] for i in db.get_path(file_item["id"])] == ["videos", "2024", "demo.mp4"]

    result = db.delete_tree(folder["id"])
    assert result["deleted"] == 3
    assert sorted(result["message_ids"]) == [11, 12]
    assert db.list_items(None) == []
    db.close()


def test_upload_record(tmp_path: Path):
    db = Database(tmp_path / "uploads.sqlite3")
    upload = db.create_upload(None, "a.bin", 100, "application/octet-stream", "tmp/a.part", 8)
    assert upload["received"] == 0
    updated = db.update_upload(upload["id"], received=8, progress=8, state="uploading")
    assert updated["received"] == 8
    assert updated["progress"] == 8
    db.update_upload(upload["id"], state="done", item_id="x")
    assert db.get_upload(upload["id"])["item_id"] == "x"
    db.close()
