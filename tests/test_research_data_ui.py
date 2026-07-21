from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_navigation_contains_data_research_center():
    source = (ROOT / "app.py").read_text()

    assert "pages/22_数据研究中心.py" in source
    assert "数据研究中心" in source


def test_research_page_has_lazy_actions_and_chinese_sections():
    source = (ROOT / "pages/22_数据研究中心.py").read_text()

    assert "数据源状态" in source
    assert "数据目录" in source
    assert "数据质量" in source
    assert "回测 V2" in source
    assert "st.form" in source
    assert "use_container_width" not in source
    assert "ALPACA_API_SECRET" not in source
