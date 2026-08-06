from pathlib import Path

from futu_quant.operational_readiness import audit_streamlit_pages


def test_streamlit_pages_do_not_render_delta_generator_from_bare_if_expression() -> None:
    result = audit_streamlit_pages(Path(__file__).parents[1])

    matching = [
        issue
        for issue in result["issues"]
        if "裸三元表达式" in issue["问题"]
    ]
    assert matching == []


def test_audit_detects_bare_streamlit_if_expression(tmp_path: Path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "app.py").write_text(
        "import streamlit as st\n"
        "rows = []\n"
        "st.dataframe(rows) if rows else st.info('empty')\n",
        encoding="utf-8",
    )

    result = audit_streamlit_pages(tmp_path)

    assert result["errors"] == 1
    assert "裸三元表达式" in result["issues"][0]["问题"]
