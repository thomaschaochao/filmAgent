from app.services.rag import chunk_script, estimate_tokens


def test_script_chunker_respects_scenes_and_dialogue_blocks():
    script = """第一幕

内景：驾驶舱 - 夜

林深：
我们必须现在降落。

林深握紧操纵杆，保持在驾驶座范围内。

外景：机场跑道 - 夜

跑道灯在雨中延伸。"""
    chunks = chunk_script(script, target_tokens=12, hard_limit=40)

    assert len(chunks) >= 3
    assert any("我们必须现在降落。" in chunk.content for chunk in chunks)
    assert all(not chunk.content.startswith("我们必须") for chunk in chunks)
    assert chunks[0].chapter == "第一幕"
    assert any(chunk.scene == "内景：驾驶舱 - 夜" for chunk in chunks)
    assert all(chunk.start_offset < chunk.end_offset for chunk in chunks)


def test_token_estimate_counts_chinese_and_words():
    assert estimate_tokens("林深 walks toward gate 3.") >= 6
