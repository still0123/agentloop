import pytest


@pytest.fixture
def workdir(tmp_path):
    """每个测试独享的临时工作区。"""
    return tmp_path
