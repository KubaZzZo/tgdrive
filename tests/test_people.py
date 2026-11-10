from datetime import timedelta

from test_album_captions import album_message
from test_channel_sync import message

from app.people import extract_people
from app.person_names import chinese_person_name
from app.services.channel_sync import describe_message


def test_caption_recognizes_chinese_names_and_prefers_explicit_credits():
    assert extract_people("刘德华与周杰伦同台演唱\nhttps://example.com/张三") == (
        "刘德华",
        "周杰伦",
    )
    assert extract_people("刘德华的歌曲\n人物：李四、Jane Doe、ＪＡＮＥ DOE") == (
        "李四",
        "Jane Doe",
    )
    assert not extract_people("北京旅游记录")


def test_people_use_shared_album_description_only_when_unambiguous():
    first = album_message(1, 100, "cover.jpg", "现场记录\n\n人物：张三")
    other = album_message(2, 100, "other.jpg", "现场记录\n\n人物：李四")
    video = album_message(3, 100, "video.mp4")
    assert describe_message(video, [first, video])["people"] == ("张三",)
    assert not describe_message(video, [first, other, video])["people"]
    video.message = "现场记录\n\n人物：王小明"
    assert describe_message(video, [first, video])["people"] == ("王小明",)


def test_video_people_follow_file_categories_for_generic_media_types():
    video = message(1, name="clip.mkv", caption="人物：张三")
    video.document.mime_type = "application/x-matroska"
    assert describe_message(video)["people"] == ("张三",)


def test_name_display_transliterates_kana_and_preserves_chinese_name_characters():
    assert chinese_person_name("Jane Doe") == "简·多伊"
    assert chinese_person_name("Morgan") == "摩根"
    assert chinese_person_name("Fletcher") == "夫莱彻"  # Pronunciation dictionary fallback.
    assert chinese_person_name("齋藤飛鳥") == "斋藤飞鸟"
    assert chinese_person_name("山田太郎") == "山田太郎"
    assert chinese_person_name("張芸") == "张芸"
    assert chinese_person_name("沢尻エリカ") == "泽尻埃里卡"
    assert chinese_person_name("さくら") == "萨库拉"
    assert chinese_person_name("Qzxyzz") == "Qzxyzz"


def test_people_history_backfill_and_edits_preserve_renames_and_respect_tombstones(app_client):
    client, app, storage = app_client
    assert client.get("/api/people").status_code == 401
    client.post("/api/login", json={"password": "correct-horse-battery-staple"})
    db, chat = app.state.db, str(storage.chat)
    first = message(1, caption="现场记录\n\n人物：张三、李四、Jane Doe")
    data = describe_message(first)
    people = data.pop("people")
    db.import_channel_file(chat, first.id, data)  # A video from before the person index.
    [item] = db.list_items(None)
    db.rename_item(item["id"], "自定义.mp4")
    assert db.import_channel_file(chat, first.id, {**data, "people": people}, "history")
    second = message(2, caption="人物：张三")
    db.import_channel_file(chat, second.id, describe_message(second))
    assert client.get("/api/people", params={"limit": 1}).json() == {
        "people": [{"name": "张三", "display_name": "张三", "count": 2}],
        "total": 3,
    }
    assert client.get("/api/people", params={"q": "ＪＡＮＥ"}).json() == {
        "people": [{"name": "Jane Doe", "display_name": "简·多伊", "count": 1}],
        "total": 1,
    }
    detail = client.get("/api/items", params={"person": "jane doe", "scope": "all"}).json()
    assert detail["person_display_name"] == "简·多伊"
    assert [row["name"] for row in detail["items"]] == ["自定义.mp4"]

    first.edit_date = first.date + timedelta(seconds=1)
    first.message = "现场记录\n\n人物：王小明"
    assert db.import_channel_file(chat, first.id, describe_message(first))
    assert not db.import_channel_file(chat, first.id, {**data, "people": people})
    detail = client.get("/api/items", params={"person": "王小明", "scope": "all"}).json()
    assert [row["name"] for row in detail["items"]] == ["自定义.mp4"]
    db.remove_channel_messages(chat, [first.id, second.id])
    assert not db.import_channel_file(chat, first.id, describe_message(first))
    assert client.get("/api/people").json() == {"people": [], "total": 0}


def test_people_can_search_transliterations_without_merging_original_names(authenticated_client):
    client, app, storage = authenticated_client
    db, chat = app.state.db, str(storage.chat)
    for mid, person in enumerate(("さくら", "サクラ", "Jane Doe"), start=1):
        post = message(mid, caption=f"第 {mid} 条视频\n\n人物：{person}")
        db.import_channel_file(chat, mid, describe_message(post))
    result = client.get("/api/people", params={"q": "萨库", "limit": 1}).json()
    assert result == {
        "people": [{"name": "さくら", "display_name": "萨库拉", "count": 1}],
        "total": 2,
    }
    second_page = client.get("/api/people", params={"q": "萨库", "limit": 1, "offset": 1}).json()
    assert second_page["people"][0]["name"] == "サクラ"
    for original, title in [("さくら", "第 1 条视频.mp4"), ("サクラ", "第 2 条视频.mp4")]:
        detail = client.get("/api/items", params={"person": original, "scope": "all"}).json()
        assert detail["person_display_name"] == "萨库拉"
        assert [row["name"] for row in detail["items"]] == [title]
