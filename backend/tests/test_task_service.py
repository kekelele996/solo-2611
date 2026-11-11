from solo_auto.pipeline.task_service import _classification


def test_classification_uses_engineering_tag() -> None:
    meta = {
        "tags": [
            {
                "id": "tag-1",
                "name": "系统安全",
                "parent_id": "parent-1",
                "parent_name": "安全",
            }
        ]
    }

    assert _classification(meta) == ("安全", "系统安全")


def test_classification_ignores_string_tags() -> None:
    assert _classification({"tags": ["数据工程"]}) == ("", "")
    assert _classification({}) == ("", "")
