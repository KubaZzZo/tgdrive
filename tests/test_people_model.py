from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from conftest import make_settings
from test_channel_sync import ChannelStorage, message

from app.infrastructure.db import Database
from app.services import people_model
from app.services.channel_sync import ChannelSyncService


def model_settings(workdir):
    return make_settings(
        workdir,
        people_ai_base_url="https://models.example/v1",
        people_ai_model="gpt-6.1-terra",
        people_ai_api_key="test-key",
    )


def test_people_worker_deduplicates_live_descriptions_and_skips_history(workdir, monkeypatch):
    async def run():
        release_model = asyncio.Event()
        model_started = asyncio.Event()
        requests = []

        async def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            assert request.url.path == "/v1/chat/completions"
            assert body["model"] == "gpt-6.1-terra"
            caption = json.loads(body["messages"][-1]["content"])["caption"]
            assert caption == "Jane Doe, also known as Janie."
            model_started.set()
            await release_model.wait()
            people = [
                {"name": "Jane Doe", "name_zh": "珍·多伊", "aliases": ["Janie", "Unlisted"]},
                {"name": "Made Up", "name_zh": "虚构", "aliases": []},
            ]
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"people": people})},
                        }
                    ]
                },
            )

        factory = httpx.AsyncClient
        monkeypatch.setattr(
            people_model.httpx,
            "AsyncClient",
            lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
        )
        db = Database(workdir / "people-ai.sqlite3")
        storage = ChannelStorage()
        history = message(1, caption="Another old description.")
        storage.history = [history]
        service = ChannelSyncService(db, storage, model_settings(workdir))
        await service.start()
        try:
            async with asyncio.timeout(3):
                while service.last_success is None:
                    await asyncio.sleep(0)
                assert requests == []
                caption = "Jane Doe, also known as Janie.\n@account https://example.invalid/secret"
                first, second = message(2, caption=caption), message(3, caption=caption)
                await service.on_message(first)
                await model_started.wait()
                await service.on_message(second)
                assert len(db.list_items(None)) == 3
                assert db.list_video_people("", 10, 0) == ([], 0)
                release_model.set()
                expected = ([{"name": "Jane Doe", "display_name": "珍·多伊", "count": 2}], 1)
                while db.list_video_people("Janie", 10, 0) != expected:
                    await asyncio.sleep(0)
                assert db.list_video_people("Made Up", 10, 0) == ([], 0)
                assert db.list_video_people("Unlisted", 10, 0) == ([], 0)
                assert db.video_person_display_name("Jane Doe") == "珍·多伊"
                storage.history += [first, second]
                await service.scan(full=True)
                assert db.list_video_people("Janie", 10, 0) == expected
                assert len(requests) == 1
        finally:
            await service.shutdown()
            db.close()

    asyncio.run(run())


def test_old_analysis_does_not_overwrite_edited_caption_and_local_credits(workdir):
    with_db = Database(workdir / "people-ai.sqlite3")
    service = ChannelSyncService(with_db, ChannelStorage(), model_settings(workdir))
    try:
        post = message(1, caption="人物：张三")
        service.ingest(post)
        old_key, _ = with_db.next_people_ai_job(service.people_model.namespace, 1)
        post.message = "人物：李四"
        post.edit_date = post.date + timedelta(seconds=1)
        service.ingest(post)
        assert (
            with_db.finish_people_ai_job(
                old_key, [{"name": "张三", "display_name": "张三", "aliases": []}]
            )
            == 0
        )
        new_key, _ = with_db.next_people_ai_job(service.people_model.namespace, 1)
        for _ in range(3):
            with_db.defer_people_ai_job(new_key, 1)
        assert with_db.next_people_ai_job(service.people_model.namespace, 2) is None
        assert (
            with_db.conn.execute(
                "SELECT caption FROM people_ai_jobs WHERE key=?", (new_key,)
            ).fetchone()["caption"]
            is None
        )
        assert with_db.list_video_people("", 10, 0) == (
            [{"name": "李四", "display_name": "李四", "count": 1}],
            1,
        )
    finally:
        with_db.close()


@pytest.mark.parametrize(
    ("caption", "model_names", "expected"),
    [
        ("给刘德华粉丝的推荐，主演为 Jane Doe", ("Jane Doe",), {"Jane Doe"}),
        ("给刘德华粉丝的推荐", (), set()),
        ("女优：山田はな、佐藤さくら\n刘德华粉丝推荐", ("山田はな",), {"山田はな", "佐藤さくら"}),
    ],
)
def test_model_replaces_guesses_and_keeps_explicit_credits_after_rescan(
    workdir, caption, model_names, expected
):
    db = Database(workdir / "people-ai.sqlite3")
    service = ChannelSyncService(db, ChannelStorage(), model_settings(workdir))
    try:
        post = message(1, caption=caption)
        service.ingest(post)
        before = {row["name"] for row in db.list_video_people("", 10, 0)[0]}
        assert before == ({"刘德华"} if "女优：" not in caption else expected)
        key, _ = db.next_people_ai_job(service.people_model.namespace, 1)
        people = [{"name": name, "display_name": "", "aliases": []} for name in model_names]
        db.finish_people_ai_job(key, people)
        assert {row["name"] for row in db.list_video_people("", 10, 0)[0]} == expected
        assert not service.ingest(post, scan_run="history")
        assert {row["name"] for row in db.list_video_people("", 10, 0)[0]} == expected
    finally:
        db.close()
