import time


def test_archive_pages_reach_all_records_with_identical_timestamps(journey):
    reader = journey.login()
    journey.subscribe(reader)
    expected = {
        journey.service.publish(
            "quantgt",
            f"same-time-{index}",
            f"Imported report {index}",
            "Historical body",
            baseline=True,
        )["id"]
        for index in range(57)
    }
    # Imported batches can legitimately preserve the same source timestamp.
    journey.service.db.execute("UPDATE versions SET created_at=?", (time.time() - 10,))
    params = {"limit": 7}
    collected = []
    for _ in range(10):
        response = reader.get("/api/library", params=params)
        assert response.status_code == 200
        page = response.json()
        if not page["contents"]:
            break
        collected.extend(content["id"] for content in page["contents"])
        params = {
            "limit": 7,
            "before": page["next_before"],
            "before_id": page["next_before_id"],
        }
    else:
        raise AssertionError("Archive cursor did not terminate")
    assert len(collected) == len(set(collected)) == 57
    assert set(collected) == expected
    assert reader.get("/api/events").json()["events"] == []
    assert journey.client().get("/api/library").status_code == 401
