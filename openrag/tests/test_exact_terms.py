from openrag.search.es_chunk_contract import (
    extract_exact_terms,
    normalize_exact_term,
)


def test_normalize_exact_term_unifies_width_case_and_whitespace():
    assert normalize_exact_term("  ＧＢ／Ｔ　３５２７３－２０２０  ") == "gb/t 35273-2020"
    assert normalize_exact_term(" Qwen3-Embedding-0.6B ") == "qwen3-embedding-0.6b"


def test_extract_exact_terms_covers_first_release_identifiers_in_source_order():
    text = (
        "依据 GB/T 35273-2020，产品 HT-2025-001 使用 "
        "Qwen3-Embedding-0.6B；配置 OPENRAG_RETRIEVAL_USE_L0_L1，"
        "错误码 ERR/A02-503.1，接口 api/v1/search，版本 v1.2.3。"
    )

    assert extract_exact_terms(text) == [
        "gb/t 35273-2020",
        "ht-2025-001",
        "qwen3-embedding-0.6b",
        "openrag_retrieval_use_l0_l1",
        "err/a02-503.1",
        "api/v1/search",
        "v1.2.3",
    ]


def test_extract_exact_terms_is_same_for_document_and_query_variants():
    document_terms = extract_exact_terms("标准号为 GB/T 35273-2020。")
    query_terms = extract_exact_terms("请查找 ｇｂ／ｔ　３５２７３－２０２０")

    assert document_terms == ["gb/t 35273-2020"]
    assert query_terms == document_terms


def test_extract_exact_terms_deduplicates_without_reordering():
    assert extract_exact_terms(
        "HT-2025-001、ht-2025-001、ERR/A02-503.1、HT-2025-001"
    ) == ["ht-2025-001", "err/a02-503.1"]


def test_extract_exact_terms_does_not_copy_natural_language_or_plain_numbers():
    assert extract_exact_terms("数据安全管理办法适用于企业日常管理。") == []
    assert extract_exact_terms("本文件发布于 2025 年，共有 128 页。") == []
    assert extract_exact_terms("普通英文 words and phrases are not identifiers") == []


def test_extract_exact_terms_enforces_length_and_count_limits():
    too_long = "MODEL-" + "A" * 129
    many = " ".join(f"ERR-{index:03d}-A02" for index in range(70))

    assert extract_exact_terms(too_long) == []
    terms = extract_exact_terms(many)
    assert len(terms) == 64
    assert terms[0] == "err-000-a02"
    assert terms[-1] == "err-063-a02"
