from solo_auto.pipeline.recording_service import parse_window_list


def test_parse_record_list_contract() -> None:
    windows = parse_window_list(
        "    123  Terminal              1920x1080  zsh — ~/work\n"
        "   8976  Google Chrome         1440x810   localhost:3000\n"
    )
    assert windows == [
        {
            "id": 123,
            "windowId": 123,
            "app": "Terminal",
            "title": "zsh — ~/work",
            "width": 1920,
            "height": 1080,
            "owner_pid": 0,
        },
        {
            "id": 8976,
            "windowId": 8976,
            "app": "Google Chrome",
            "title": "localhost:3000",
            "width": 1440,
            "height": 810,
            "owner_pid": 0,
        },
    ]
