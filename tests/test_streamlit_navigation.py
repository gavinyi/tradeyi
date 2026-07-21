from pathlib import Path
import tomllib


def test_sidebar_navigation_is_enabled() -> None:
    config_path = Path(__file__).parents[1] / ".streamlit" / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))

    assert config.get("client", {}).get("showSidebarNavigation", True) is True


def test_pages_do_not_link_directly_to_navigation_entrypoint() -> None:
    pages_dir = Path(__file__).parents[1] / "pages"
    offenders = [
        path.name
        for path in pages_dir.glob("*.py")
        if 'st.page_link("app.py"' in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
