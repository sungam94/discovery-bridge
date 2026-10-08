import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures" / "spike"


@pytest.fixture
def fixture():
    def load(name: str):
        return json.loads((FIX / f"{name}.json").read_text())
    return load
