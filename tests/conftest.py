import pytest

from rotengine.content import load_content


@pytest.fixture(scope="session")
def content():
    return load_content()


@pytest.fixture(scope="session")
def modded_content():
    return load_content(["example_mod"])
