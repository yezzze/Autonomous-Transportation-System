#!/usr/bin/env python3
"""
分布式系统独立测试脚本
绕过原始 agents/tools 依赖，直接测试分布式调度流程
"""

import sys
import os
from typing import TypedDict
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置环境变量
os.environ['REASONING_API_KEY'] = os.getenv('REASONING_API_KEY', 'sk-31be252e9dcf413ea4a9ac05ac8b4594')
os.environ['REASONING_BASE_URL'] = os.getenv('REASONING_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
os.environ['REASONING_MODEL'] = os.getenv('REASONING_MODEL', 'qwq-plus')
os.environ['BASIC_API_KEY'] = os.getenv('BASIC_API_KEY', 'sk-31be252e9dcf413ea4a9ac05ac8b4594')
os.environ['BASIC_BASE_URL'] = os.getenv('BASIC_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
os.environ['BASIC_MODEL'] = os.getenv('BASIC_MODEL', 'qwen-max-latest')

from src.graph.distributed_builder import build_distributed_graph
from src.graph.distributed_types import DistributedState
from langchain_core.messages import HumanMessage


def test_distributed_workflow():
    """测试分布式工作流"""
    
    print("=" * 80)
    print("分布式代理调度系统 - 测试运行")
    print("=" * 80)
    
    # 构建图
    print("\n[1/4] 构建分布式图...")
    graph = build_distributed_graph()
    print("✓ 图构建完成")
    
    # 初始化状态
    user_query = "我要做一个城市的红绿灯调度" # DeepSeek R1 最新消息，并总结要点"
    print(f"\n[2/4] 用户查询: {user_query}")
    
    initial_state: DistributedState = {
        "messages": [HumanMessage(content=user_query)],
        "execution_plan": [],
        "agent_registry_cache": []
    }
    
    # 执行工作流
    print("\n[3/4] 开始执行分布式工作流...\n")
    
    try:
        result = graph.invoke(initial_state)
        
        print("\n[4/4] 执行完成！")
        print("=" * 80)
        print("执行计划:")
        for i, task in enumerate(result.get("execution_plan", []), 1):
            print(f"\n任务 {i}:")
            print(f"  - ID: {task.get('task_id', 'N/A')}")
            print(f"  - 代理: {task.get('agent_id', 'N/A')}")
            print(f"  - 地址: {task.get('target_ip', 'N/A')}:{task.get('target_port', 'N/A')}")
            print(f"  - 描述: {task.get('task_description', 'N/A')[:60]}...")
            print(f"  - 状态: {task.get('status', 'N/A')}")
            if task.get('error'):
                print(f"  - 错误: {task['error']}")
            if task.get('result'):
                result_preview = str(task['result'])[:100]
                print(f"  - 结果: {result_preview}...")
        
        print("\n" + "=" * 80)
        print("最终报告:")
        # 显示最后一条消息（Reporter 生成的报告）
        final_report = result.get("messages", [])
        # if final_report:
        #     print("\n" + final_report.content)
        for msg in final_report:
            if msg:
                print("\n" + msg.content)
        
        print("\n" + "=" * 80)
        print("✓ 测试完成！分布式调度系统运行正常！")
        
    except Exception as e:
        print(f"\n✗ 执行失败: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(test_distributed_workflow())
