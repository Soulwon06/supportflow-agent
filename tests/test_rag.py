from app.graph import app
from app.state import create_initial_state


def test_knowledge_flow():
    state = create_initial_state(
        user_message="商品有质量问题应该怎么处理？",
        role="customer_service",
    )

    config = {
        "configurable": {
            "thread_id": "test-knowledge-flow"
        }
    }

    result = app.invoke(
        state,
        config=config,
    )

    assert result["intent"] == "knowledge"
    assert result["result"]
    assert result["error_log"] == ""