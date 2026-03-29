#!/usr/bin/env python3
"""
测试系统自我介绍功能
"""

import sys
import os
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['REASONING_API_KEY'] = os.getenv('REASONING_API_KEY', 'sk-31be252e9dcf413ea4a9ac05ac8b4594')
os.environ['REASONING_BASE_URL'] = os.getenv('REASONING_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
os.environ['REASONING_MODEL'] = os.getenv('REASONING_MODEL', 'qwq-plus')
os.environ['BASIC_API_KEY'] = os.getenv('BASIC_API_KEY', 'sk-31be252e9dcf413ea4a9ac05ac8b4594')
os.environ['BASIC_BASE_URL'] = os.getenv('BASIC_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
os.environ['BASIC_MODEL'] = os.getenv('BASIC_MODEL', 'qwen-max-latest')

from src.graph.distributed_builder import build_distributed_graph
from src.graph.distributed_types import DistributedState
from langchain_core.messages import HumanMessage


def test_system_questions():
    """测试系统对自身介绍类问题的处理"""
    
    test_queries = [
        "你是谁？",
        "你可以做什么？",
        "你有哪些能力？",
        "介绍一下你自己",
        "你能帮我做什么？"
    ]
    
    graph = build_distributed_graph()
    
    for query in test_queries:
        print("=" * 80)
        print(f"测试问题: {query}")
        print("=" * 80)
        
        initial_state: DistributedState = {
            "messages": [HumanMessage(content=query)],
            "execution_plan": [],
            "agent_registry_cache": []
        }
        
        try:
            result = graph.invoke(initial_state)
            
            # 获取最后一条消息（系统回答）
            final_message = result.get("messages", [])[-1]
            
            print("\n【系统回答】:")
            print(final_message.content)
            print("\n")
            
            # 检查是否生成了任务
            execution_plan = result.get("execution_plan", [])
            if len(execution_plan) > 0:
                print(f"⚠️  注意：生成了 {len(execution_plan)} 个任务（预期为0）")
            else:
                print("✅ 正确：没有生成任务，直接回答")
                
        except Exception as e:
            print(f"❌ 执行失败: {e}")
            import traceback
            traceback.print_exc()
        
        print("\n")


if __name__ == "__main__":
    test_system_questions()
