from pathlib import Path

import pytest

from solo_auto.instance_lock import AlreadyRunningError, InstanceLock


def test_second_instance_is_rejected(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "run" / "instance.lock", tmp_path / "run" / "pid")
    second = InstanceLock(tmp_path / "run" / "instance.lock", tmp_path / "run" / "pid")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()
