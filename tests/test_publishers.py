"""Издатели на подменённом HTTP: без сети и без настоящих публикаций."""

import httpx
import pytest

from smm.publishers import PublishError
from smm.publishers.threads import ThreadsPublisher


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("smm.publishers.threads.asyncio.sleep", fast)


def threads_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_threads_waits_while_container_not_found_then_publishes_chain():
    calls = []
    status_checks = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path, params = req.url.path, dict(req.url.params)
        calls.append((req.method, path, params.get("reply_to_id")))
        if req.method == "POST" and path.endswith("/threads"):
            return httpx.Response(200, json={"id": f"c{len(calls)}"})
        if req.method == "GET" and params.get("fields", "").startswith("status"):
            status_checks["n"] += 1
            if status_checks["n"] == 1:  # Meta ещё не «видит» только что созданный контейнер
                return httpx.Response(400, json={"error": {"message": "The requested resource does not exist", "code": 100}})
            return httpx.Response(200, json={"status": "FINISHED"})
        if path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": f"p{len(calls)}"})
        if params.get("fields") == "permalink":
            return httpx.Response(200, json={"permalink": "https://www.threads.com/@mikheenkov/post/X"})
        return httpx.Response(404, json={"error": {"message": "unexpected"}})

    pub = ThreadsPublisher("42", "tok", client=threads_client(handler))
    res = await pub.publish({"parts": ["первая", "вторая"], "link_attachment_url": None, "topic_tag": "ИИ", "mode": "chain"})
    assert res.url == "https://www.threads.com/@mikheenkov/post/X"
    assert len(res.extra["part_ids"]) == 2
    # вторая часть — ответ на первую
    replies = [r for m, p, r in calls if m == "POST" and p.endswith("/threads") and r]
    assert replies == [res.extra["part_ids"][0]]


async def test_threads_resumes_chain_from_saved_progress():
    created = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        if req.method == "POST" and req.url.path.endswith("/threads"):
            created.append(params["text"])
            return httpx.Response(200, json={"id": "c"})
        if params.get("fields", "").startswith("status"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if req.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "p2"})
        return httpx.Response(200, json={"permalink": "https://t/1"})

    pub = ThreadsPublisher("42", "tok", client=threads_client(handler))
    res = await pub.publish({"parts": ["первая", "вторая"]}, done={"part_ids": ["p1"]})
    assert created == ["вторая"] and res.extra["part_ids"] == ["p1", "p2"]


async def test_threads_real_error_is_raised_with_progress():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "Invalid parameter", "code": 100}})

    pub = ThreadsPublisher("42", "tok", client=threads_client(handler))
    with pytest.raises(PublishError) as exc:
        await pub.publish({"parts": ["x"]})
    assert exc.value.extra == {"part_ids": []}


async def test_threads_separate_posts_are_independent_with_card_on_post_with_link():
    created = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        if req.method == "POST" and req.url.path.endswith("/threads"):
            created.append(params)
            return httpx.Response(200, json={"id": f"c{len(created)}"})
        if params.get("fields", "").startswith("status"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if req.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": f"p{len(created)}"})
        return httpx.Response(200, json={"permalink": f"https://t/{req.url.path.rsplit('/', 1)[-1]}"})

    pub = ThreadsPublisher("42", "tok", client=threads_client(handler))
    res = await pub.publish({"parts": ["Мысль без ссылки", "Мысль про сайт (https://40db.es)"],
                             "link_attachment_url": "https://40db.es", "topic_tags": ["Нейминг", "Бренд"], "mode": "separate"})
    assert all("reply_to_id" not in c for c in created)               # отдельные посты, не ветка
    assert "link_attachment" not in created[0] and created[1]["link_attachment"] == "https://40db.es"
    assert [c["topic_tag"] for c in created] == ["Нейминг", "Бренд"]  # свой хэштег у каждого поста
    assert res.extra["urls"] == ["https://t/p1", "https://t/p2"]


async def test_threads_first_post_gets_carousel_from_several_photos():
    created = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        if req.method == "POST" and req.url.path.endswith("/threads"):
            created.append(params)
            return httpx.Response(200, json={"id": f"c{len(created)}"})
        if params.get("fields", "").startswith("status"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if req.url.path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": f"p{len(created)}"})
        return httpx.Response(200, json={"permalink": "https://t/x"})

    pub = ThreadsPublisher("42", "tok", client=threads_client(handler))
    await pub.publish({"parts": ["Пост с фото", "Второй пост (https://40db.es)"], "link_attachment_url": "https://40db.es",
                       "mode": "separate"}, image_urls=["https://smm/m/a.jpg", "https://smm/m/b.jpg"])
    items = [c for c in created if c.get("is_carousel_item") == "true"]
    carousel = next(c for c in created if c.get("media_type") == "CAROUSEL")
    assert len(items) == 2 and carousel["children"] == "c1,c2" and carousel["text"] == "Пост с фото"
    second = created[-1]
    assert second["media_type"] == "TEXT" and second["link_attachment"] == "https://40db.es"
