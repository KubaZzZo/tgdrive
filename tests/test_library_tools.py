from datetime import UTC, datetime, timedelta


def add_file(db, storage, name, message, *, parent=None, size=8):
    storage.files[message] = b"x" * size
    return db.create_file(
        name,
        parent,
        size,
        None,
        None,
        str(storage.chat),
        [{"part_index": 0, "tg_message_id": message, "size": size}],
    )


def test_trash_hides_subtree_and_pauses_links_until_restored(authenticated_client):
    client, app, storage = authenticated_client
    db = app.state.db
    folder = db.create_folder("folder", None)["id"]
    child = db.create_folder("nested", folder)["id"]
    item = add_file(db, storage, "sample.txt", 201, parent=child)["id"]
    token = client.post(f"/api/items/{item}/share", json={"expires_hours": 24}).json()["token"]
    public_path = f"/s/{token}"
    assert client.get(public_path).headers["cache-control"] == "private, no-store"
    assert client.get(public_path + "/download").headers["cache-control"] == "private, no-store"

    assert client.delete(f"/api/trash/{folder}").status_code == 404
    assert client.delete(f"/api/items/{folder}").json()["deleted"] == 3
    assert client.get("/api/items?scope=all").json()["total"] == 0
    assert client.get("/api/items", params={"parent_id": child}).status_code == 404
    assert client.get(f"/api/files/{item}/download").status_code == 404
    assert client.get(public_path).status_code == 404
    assert client.get(public_path + "/download").status_code == 404
    assert client.post(f"/api/items/{item}/share", json={}).status_code == 404
    assert client.post("/api/folders", json={"name": "new", "parent_id": child}).status_code == 404
    assert client.get("/api/shares").json()["shares"][0]["suspended"]
    assert storage.deleted_message_ids == []
    assert [entry["id"] for entry in client.get("/api/trash").json()["items"]] == [folder]
    assert client.post(f"/api/trash/{child}/restore").status_code == 404

    assert client.post(f"/api/trash/{folder}/restore").status_code == 200
    assert db.get_item(item)["parent_id"] == child
    assert client.get(public_path).status_code == 200
    assert client.get("/api/trash").json()["total"] == 0


def test_trash_expiry_and_empty_keep_independent_entries(authenticated_client):
    client, app, storage = authenticated_client
    db = app.state.db
    folder = db.create_folder("folder", None)["id"]
    child = add_file(db, storage, "child.txt", 202, parent=folder)["id"]
    another = add_file(db, storage, "other.txt", 203)["id"]
    for item_id in (child, folder, another):
        assert client.delete(f"/api/items/{item_id}").status_code == 200
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with db.conn:
        db.conn.execute("UPDATE items SET trashed_at=? WHERE id=?", (old, folder))
    cleanup = app.state.accounts.current.cleanup
    assert cleanup.purge_trash(cutoff, single_batch=True) == 1
    assert db.get_item(folder, include_trash=True) is None
    assert client.post(f"/api/trash/{child}/restore").json()["parent_id"] is None
    assert client.delete("/api/trash").json()["deleted"] == 1
    client.portal.call(cleanup.process)
    assert storage.deleted_message_ids == [203]
    assert 202 in storage.files
    assert client.get("/api/trash").json()["total"] == 0


def test_share_management_extends_and_revokes_existing_token(authenticated_client):
    client, app, storage = authenticated_client
    item = add_file(app.state.db, storage, "private.txt", 204)["id"]
    share = client.post(f"/api/items/{item}/share", json={"expires_hours": 1}).json()
    token = share["token"]
    expiry = datetime.fromisoformat(share["expires_at"])
    assert timedelta(minutes=55) < expiry - datetime.now(UTC) <= timedelta(hours=1)
    assert client.get("/api/shares").json()["shares"][0]["name"] == "private.txt"
    assert client.get("/api/shares", headers={"X-Workspace-ID": "other"}).status_code == 409
    with app.state.db.conn:
        app.state.db.conn.execute(
            "UPDATE shares SET expires_at=? WHERE token=?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), token),
        )
    public_path = f"/s/{token}"
    assert client.get(public_path).status_code == 404
    assert client.patch("/api/shares", json={"token": token, "expires_hours": 0}).json() == {
        "expires_at": None
    }
    assert client.get(public_path).status_code == 200
    assert client.request("DELETE", "/api/shares", json={"token": token}).status_code == 200
    assert client.get(public_path + "/download").status_code == 404
    client.post("/api/logout")
    assert client.get("/api/shares").status_code == 401
    assert client.request("DELETE", "/api/shares", json={"token": token}).status_code == 401


def test_batch_returns_each_item_result_and_supports_root(authenticated_client):
    client, app, storage = authenticated_client
    db = app.state.db
    folder = db.create_folder("folder", None)["id"]
    nested = db.create_folder("nested", folder)["id"]
    item = add_file(db, storage, "report.txt", 205)["id"]
    result = client.post(
        "/api/items/batch",
        json={
            "action": "move",
            "ids": [folder, item, "missing"],
            "parent_id": nested,
        },
    ).json()["results"]
    assert [(row["id"], row["ok"]) for row in result] == [
        (folder, False),
        (item, True),
        ("missing", False),
    ]
    assert "子目录" in result[0]["error"]
    assert db.get_item(item)["parent_id"] == nested
    assert client.post("/api/items/batch", json={"action": "move", "ids": [item]}).json()[
        "results"
    ][0]["ok"]
    assert db.get_item(item)["parent_id"] is None
    result = client.post(
        "/api/items/batch",
        json={
            "action": "trash",
            "ids": [nested, folder, "missing"],
        },
    ).json()["results"]
    assert [row["ok"] for row in result] == [True, True, False]
    assert client.get("/api/trash").json()["total"] == 1
    assert client.post(f"/api/trash/{folder}/restore").status_code == 200
    assert db.get_item(nested)["parent_id"] == folder


def test_folder_paths_reuse_prefixes_and_reject_invalid_batch(authenticated_client):
    client, app, _ = authenticated_client
    db = app.state.db
    parent = db.create_folder("destination", None)["id"]
    payload = {"parent_id": parent, "paths": [["album", "day 1"], ["album"], ["album", "day 2"]]}
    response = client.post("/api/folders/paths", json=payload)
    first, album, second = response.json()["ids"]
    assert db.get_item(first)["parent_id"] == album == db.get_item(second)["parent_id"]
    assert db.get_item(album)["parent_id"] == parent
    assert client.post("/api/folders/paths", json=payload).json()["ids"] == [first, album, second]
    bad = {**payload, "paths": [["do not create"], [".."]]}
    assert client.post("/api/folders/paths", json=bad).status_code == 400
    assert [row["name"] for row in db.list_items(parent)] == ["album"]
    spaced = client.post(
        "/api/folders/paths", json={**payload, "paths": [["album "], ["album"]]}
    ).json()["ids"]
    assert spaced[0] != spaced[1] == album
    assert db.get_item(spaced[0])["name"] == "album "


def test_advanced_search_composes_ranges_extension_and_scope(authenticated_client):
    client, app, storage = authenticated_client
    db = app.state.db
    folder = db.create_folder("reports", None)["id"]
    inside = add_file(db, storage, "Report.PDF", 206, parent=folder, size=128)["id"]
    outside = add_file(db, storage, "Report.pdf", 207, size=256)["id"]
    old = add_file(db, storage, "Report.pdf", 208, parent=folder, size=256)["id"]
    add_file(db, storage, "Report.pdf.txt", 209, parent=folder, size=256)
    with db.conn:
        db.conn.execute(
            "UPDATE items SET created_at='2025-01-03T07:00:00+08:00' WHERE id IN (?,?)",
            (inside, outside),
        )
        db.conn.execute("UPDATE items SET created_at='2025-01-01T00:00:00Z' WHERE id=?", (old,))
    params = {
        "parent_id": folder,
        "q": "report",
        "extension": ".pdf",
        "created_from": "2025-01-02",
        "created_to": "2025-01-02",
        "min_size": 128,
        "max_size": 256,
    }
    current = client.get("/api/items", params={**params, "scope": "folder"}).json()
    assert [row["id"] for row in current["items"]] == [inside]
    whole = client.get("/api/items", params={**params, "scope": "all"}).json()
    assert {row["id"] for row in whole["items"]} == {inside, outside}
    assert whole["path"][-1]["id"] == folder
    assert client.get("/api/items", params={"min_size": 3, "max_size": 2}).status_code == 400
    assert (
        client.get(
            "/api/items", params={"created_from": "2025-01-03", "created_to": "2025-01-02"}
        ).status_code
        == 400
    )
