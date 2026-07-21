from pathlib import Path

from futu_quant.ui import MOBILE_RESPONSIVE_CSS


ROOT = Path(__file__).resolve().parents[1]


def test_shared_mobile_styles_cover_navigation_actions_tables_and_tabs():
    assert "@media (max-width: 768px)" in MOBILE_RESPONSIVE_CSS
    assert 'data-testid="stSidebar"' in MOBILE_RESPONSIVE_CSS
    assert '[data-testid="stSidebar"][aria-expanded="false"]' in MOBILE_RESPONSIVE_CSS
    assert 'data-testid="stSidebarCollapsedControl"' in MOBILE_RESPONSIVE_CSS
    assert 'data-testid="stButton"' in MOBILE_RESPONSIVE_CSS
    assert 'data-testid="stDataFrame"' in MOBILE_RESPONSIVE_CSS
    assert 'data-baseweb="tab-list"' in MOBILE_RESPONSIVE_CSS
    assert 'data-testid="stSegmentedControl"' in MOBILE_RESPONSIVE_CSS
    assert '[data-testid="stHorizontalBlock"] > div' in MOBILE_RESPONSIVE_CSS
    assert "min-height: 44px" in MOBILE_RESPONSIVE_CSS


def test_every_streamlit_page_loads_shared_responsive_entry_point():
    page_sources = [
        (path, path.read_text()) for path in sorted((ROOT / "pages").glob("*.py"))
    ]

    missing = [str(path.name) for path, source in page_sources if "render_sidebar(" not in source]

    assert not missing
    assert "render_sidebar(settings)" in (ROOT / "app.py").read_text()
    assert 'initial_sidebar_state="auto"' in (ROOT / "app.py").read_text()
    assert "expanded=True" not in (ROOT / "app.py").read_text()
